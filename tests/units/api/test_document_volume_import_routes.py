"""Route contracts for Knowledge Store import from Unity Catalog Volumes.

The Files API is mocked at the HTTP-session level and the real
``get_data_plane_client`` → ``DatabricksClient`` → ``VolumeFileService``
chain runs in Databricks App mode, so these tests prove the forwarded user
token (not the app service principal) is the bearer on every volume call —
including calls made later by the background import task.
"""

from __future__ import annotations

import importlib
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import unquote

import pytest
import requests

from api.routers.internal import domain as routes
from api.routers.internal import tasks as task_routes
from back.core.databricks import DatabricksAuth, ParseStatus, ParseSubmission
from back.core.databricks.request_identity import (
    RequestIdentity,
    reset_request_identity,
    set_request_identity,
)
from back.core.errors import AuthorizationError, ValidationError
from back.core.task_manager import get_task_manager

pytestmark = pytest.mark.unit

FOLDER, VERSION = "sales", "1"
ROOT = "/Volumes/main/docs/raw"
FILES_PREFIX = "https://ws.example.com/api/2.0/fs/files"
DIRS_PREFIX = "https://ws.example.com/api/2.0/fs/directories"
SETTINGS = SimpleNamespace(databricks_host="", databricks_token="")


class _Request:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _FakeParseService:
    """``pending=True`` makes binaries go through ``parse_pending`` (inline)."""

    def __init__(self, pending=False):
        self.pending = pending
        self.prepared = []
        self.parsed = []

    def prepare_upload(self, folder, version, filename, content):
        self.prepared.append((folder, version, filename, content))
        if self.pending:
            return ParseSubmission(filename, True, False, True, ParseStatus.PENDING)
        return ParseSubmission(filename, True, False, False, ParseStatus.READY)

    def parse_pending(self, folder, version, filename):
        self.parsed.append(filename)
        return {"filename": filename, "status": ParseStatus.READY.value}


class _FilesAPI:
    """Mocked Files API keyed by volume path; records every bearer token."""

    def __init__(self, files=None, dirs=None, forbidden=(), expired=()):
        self.files = files or {}
        self.dirs = dirs or {}
        self.forbidden = set(forbidden)
        self.expired = set(expired)  # 401 on download only (token aged mid-job)
        self.bearers = []
        self.downloads = []

    @staticmethod
    def _resp(status, *, headers=None, payload=None, body=b""):
        resp = MagicMock(status_code=status, headers=headers or {})
        resp.json.return_value = payload or {}
        resp.iter_content.side_effect = lambda **_kw: iter([body] if body else [])
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    def head(self, url, headers=None, **_kw):
        self.bearers.append(headers["Authorization"])
        path = unquote(url[len(FILES_PREFIX):])
        if path in self.forbidden:
            return self._resp(403)
        if path in self.files:
            return self._resp(200, headers={"content-length": str(len(self.files[path]))})
        return self._resp(404)

    def get(self, url, headers=None, **_kw):
        self.bearers.append(headers["Authorization"])
        if url.startswith(DIRS_PREFIX):
            path = unquote(url[len(DIRS_PREFIX):])
            if path not in self.dirs:
                return self._resp(404)
            return self._resp(200, payload={"contents": self.dirs[path]})
        path = unquote(url[len(FILES_PREFIX):])
        self.downloads.append(path)
        if path in self.expired:
            return self._resp(401)
        if path in self.forbidden:
            return self._resp(403)
        return self._resp(200, body=self.files[path])


class _Tasks:
    """Real TaskManager, but background targets run when the test says so."""

    def __init__(self):
        self.real = get_task_manager()
        self.pending = []

    def run_background_task(self, name, task_type, target, *args, steps=None, **kwargs):
        task = self.real.create_task(name, task_type, steps=steps)
        self.pending.append((task, target, args, kwargs))
        return task

    def run_pending(self):
        """Run queued workers on a fresh thread (no request context)."""
        for task, target, args, kwargs in self.pending:
            worker = threading.Thread(target=target, args=(task, *args), kwargs=kwargs)
            worker.start()
            worker.join(timeout=30)
        self.pending.clear()

    def __getattr__(self, name):
        return getattr(self.real, name)


@pytest.fixture
def app_mode(monkeypatch):
    """Databricks App runtime: SP credentials present, user token forwarded."""
    for key in ("DATABRICKS_TOKEN", "DATABRICKS_HOST"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8000")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-id")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "sp-secret")
    monkeypatch.setattr(DatabricksAuth, "get_oauth_token", lambda self: "SP_TOKEN")

    helpers = importlib.import_module("back.core.helpers.DatabricksHelpers")
    monkeypatch.setattr(helpers, "_domain_databricks", lambda d: {"host": "https://ws.example.com"})
    monkeypatch.setattr(helpers.DatabricksHelpers, "resolve_warehouse_id", lambda d, s: "wh1")
    monkeypatch.setattr(helpers.DatabricksHelpers, "resolve_use_cloud_fetch", lambda d, s: False)

    domain = SimpleNamespace(
        uc_domain_folder=FOLDER, domain_folder=FOLDER, current_version=VERSION
    )
    monkeypatch.setattr(routes, "get_domain", lambda _m: domain)
    ctx = SimpleNamespace(domain=domain, monkeypatch=monkeypatch, tasks=_Tasks())
    ctx.parse_service = _FakeParseService()
    monkeypatch.setattr(routes, "_make_document_parse_service", lambda *_a, **_k: ctx.parse_service)
    monkeypatch.setattr(routes, "get_task_manager", lambda: ctx.tasks)

    token = set_request_identity(RequestIdentity(email="u@x.com", user_token="USER_TOKEN"))
    yield ctx
    reset_request_identity(token)


def _use_files_api(ctx, api):
    ctx.monkeypatch.setattr(requests.Session, "head", lambda self, url, **kw: api.head(url, **kw))
    ctx.monkeypatch.setattr(requests.Session, "get", lambda self, url, **kw: api.get(url, **kw))


async def _start(paths):
    return await routes.import_volume_documents(
        _Request({"paths": paths}), SimpleNamespace(), SETTINGS
    )


async def _finish(ctx, task_id):
    """Run the worker with *no* request identity, then read GET /tasks/{id}."""
    blank = set_request_identity(RequestIdentity())
    try:
        ctx.tasks.run_pending()
    finally:
        reset_request_identity(blank)
    return (await task_routes.get_task(task_id))["task"]


# -- task lifecycle ---------------------------------------------------------


@pytest.mark.asyncio
async def test_post_returns_task_id_before_any_volume_call(app_mode):
    api = _FilesAPI(files={f"{ROOT}/guide.md": b"# Guide"})
    _use_files_api(app_mode, api)

    response = await _start([f"{ROOT}/guide.md"])

    assert response["success"] is True
    assert response["task_id"]
    assert response["max_files"] == 500
    assert response["batch_size"] == 50
    assert api.bearers == []  # nothing ran inside the request
    assert app_mode.tasks.real.get_task(response["task_id"]).task_type == "knowledge_store_import"


@pytest.mark.asyncio
async def test_task_reads_with_the_token_captured_at_request_time(app_mode):
    api = _FilesAPI(files={f"{ROOT}/guide.md": b"# Guide"})
    _use_files_api(app_mode, api)
    response = await _start([f"{ROOT}/guide.md"])

    task = await _finish(app_mode, response["task_id"])

    assert task["status"] == "completed"
    assert task["result"]["imported"] == 1
    assert app_mode.parse_service.prepared == [(FOLDER, VERSION, "guide.md", b"# Guide")]
    assert api.bearers and set(api.bearers) == {"Bearer USER_TOKEN"}


@pytest.mark.asyncio
async def test_progress_and_summary_are_on_the_task(app_mode):
    contents = [
        {"name": f"d{i:03d}.md", "is_directory": False, "file_size": 2} for i in range(120)
    ]
    files = {f"{ROOT}/d{i:03d}.md": b"aa" for i in range(120)}
    _use_files_api(app_mode, _FilesAPI(files=files, dirs={ROOT: contents}))
    response = await _start([ROOT])

    task = await _finish(app_mode, response["task_id"])

    result = task["result"]
    assert task["progress"] == 100
    assert (result["processed"], result["total"]) == (120, 120)
    assert (result["imported"], result["failed"]) == (120, 0)
    assert (result["batches"], result["batches_done"]) == (3, 3)
    assert task["log_entries"]  # human-readable progress lines for the tracker


@pytest.mark.asyncio
async def test_binary_files_are_parsed_inline_not_as_extra_tasks(app_mode):
    app_mode.parse_service = _FakeParseService(pending=True)
    _use_files_api(app_mode, _FilesAPI(files={f"{ROOT}/spec.pdf": b"%PDF"}))
    before = len(app_mode.tasks.real.get_all_tasks())
    response = await _start([f"{ROOT}/spec.pdf"])

    task = await _finish(app_mode, response["task_id"])

    assert app_mode.parse_service.parsed == ["spec.pdf"]
    assert task["result"]["imported"] == 1
    # Only the import task itself was created — no per-file parse tasks.
    assert len(app_mode.tasks.real.get_all_tasks()) <= before + 1


@pytest.mark.asyncio
async def test_expired_token_stops_the_task_and_lists_remaining_files(app_mode):
    path = f"{ROOT}/late.pdf"
    _use_files_api(app_mode, _FilesAPI(files={path: b"%PDF"}, expired={path}))
    response = await _start([path])

    task = await _finish(app_mode, response["task_id"])

    result = task["result"]
    assert task["status"] == "completed"
    assert "sign-in expired" in result["stopped_reason"]
    assert result["remaining"] == [path]
    assert result["imported"] == 0
    assert app_mode.parse_service.prepared == []


@pytest.mark.asyncio
async def test_forbidden_file_is_a_reported_failure(app_mode):
    path = f"{ROOT}/secret.pdf"
    api = _FilesAPI(files={path: b"%PDF"}, forbidden={path})
    _use_files_api(app_mode, api)
    response = await _start([path])

    task = await _finish(app_mode, response["task_id"])

    failure = task["result"]["failures"][0]
    assert failure["source_path"] == path
    assert "permission" in failure["message"]
    assert app_mode.parse_service.prepared == []
    assert api.downloads == []


# -- request-time validation ------------------------------------------------


@pytest.mark.asyncio
async def test_missing_user_token_fails_closed_before_any_volume_call(app_mode):
    api = _FilesAPI(files={f"{ROOT}/a.md": b"a"})
    _use_files_api(app_mode, api)
    token = set_request_identity(RequestIdentity(email="u@x.com", user_token=""))
    try:
        with pytest.raises(AuthorizationError):
            await _start([f"{ROOT}/a.md"])
        with pytest.raises(AuthorizationError):
            await routes.browse_document_volume(ROOT, SimpleNamespace(), SETTINGS)
    finally:
        reset_request_identity(token)
    assert app_mode.tasks.pending == []
    assert api.bearers == []


@pytest.mark.asyncio
async def test_unsaved_domain_is_rejected(app_mode):
    app_mode.domain.uc_domain_folder = ""
    app_mode.domain.domain_folder = ""
    with pytest.raises(ValidationError):
        await _start([f"{ROOT}/a.md"])


@pytest.mark.asyncio
async def test_bad_request_shape_is_rejected_without_a_task(app_mode):
    for body in ([], [" "], [f"{ROOT}/{i}.md" for i in range(51)]):
        with pytest.raises(ValidationError):
            await _start(body)
    assert app_mode.tasks.pending == []


@pytest.mark.asyncio
async def test_invalid_path_is_reported_without_calling_the_files_api(app_mode):
    api = _FilesAPI()
    _use_files_api(app_mode, api)
    response = await _start(["/Volumes/main/docs/raw/../../other/x.md"])

    task = await _finish(app_mode, response["task_id"])

    assert task["result"]["failed"] == 1
    assert api.bearers == []


# -- browse / gating --------------------------------------------------------


@pytest.mark.asyncio
async def test_browse_lists_folder_as_user(app_mode):
    api = _FilesAPI(
        dirs={ROOT: [{"name": "x.pdf", "is_directory": False, "file_size": 3}]}
    )
    _use_files_api(app_mode, api)

    result = await routes.browse_document_volume(ROOT, SimpleNamespace(), SETTINGS)

    assert result["success"] is True
    assert result["items"][0]["name"] == "x.pdf"
    assert result["items"][0]["supported"] is True
    assert api.bearers == ["Bearer USER_TOKEN"]


def test_import_is_status_gated_and_browse_is_not():
    from shared.fastapi.main import _is_status_gated_edit, _viewer_write_blocked

    assert _is_status_gated_edit("/domain/documents/import-volume", "POST") is True
    assert _viewer_write_blocked("POST", "/domain/documents/import-volume") is True
    assert _is_status_gated_edit("/domain/documents/volume/browse", "GET") is False
