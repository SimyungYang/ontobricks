"""Route contracts for Knowledge Store import from Unity Catalog Volumes.

The Files API is mocked at the HTTP-session level and the real
``get_data_plane_client`` → ``DatabricksClient`` → ``VolumeFileService``
chain runs in Databricks App mode, so these tests prove the forwarded user
token (not the app service principal) is the bearer on every volume call.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import unquote

import pytest
import requests

from api.routers.internal import domain as routes
from back.core.databricks import DatabricksAuth, ParseStatus, ParseSubmission
from back.core.databricks.request_identity import (
    RequestIdentity,
    reset_request_identity,
    set_request_identity,
)
from back.core.errors import AuthorizationError, ValidationError

pytestmark = pytest.mark.unit

FOLDER, VERSION = "sales", "1"
ROOT = "/Volumes/main/docs/raw"
FILES_PREFIX = "https://ws.example.com/api/2.0/fs/files"
DIRS_PREFIX = "https://ws.example.com/api/2.0/fs/directories"


class _Request:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _FakeParseService:
    def __init__(self):
        self.prepared = []

    def prepare_upload(self, folder, version, filename, content):
        self.prepared.append((folder, version, filename, content))
        return ParseSubmission(filename, True, False, False, ParseStatus.READY)


class _FilesAPI:
    """Mocked Files API keyed by absolute volume path; records bearer tokens."""

    def __init__(self, files=None, dirs=None, forbidden=()):
        self.files = files or {}
        self.dirs = dirs or {}
        self.forbidden = set(forbidden)
        self.bearers = []
        self.downloads = []

    def _resp(self, status, *, headers=None, payload=None, body=b""):
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
            if path in self.forbidden:
                return self._resp(403)
            if path not in self.dirs:
                return self._resp(404)
            return self._resp(200, payload={"contents": self.dirs[path]})
        path = unquote(url[len(FILES_PREFIX):])
        self.downloads.append(path)
        if path in self.forbidden:
            return self._resp(403)
        return self._resp(200, body=self.files[path])


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
    parse_service = _FakeParseService()
    monkeypatch.setattr(routes, "_make_document_parse_service", lambda *_a, **_k: parse_service)
    monkeypatch.setattr(routes, "get_task_manager", lambda: MagicMock())

    token = set_request_identity(RequestIdentity(email="u@x.com", user_token="USER_TOKEN"))
    yield SimpleNamespace(domain=domain, parse_service=parse_service, monkeypatch=monkeypatch)
    reset_request_identity(token)


def _use_files_api(ctx, api):
    ctx.monkeypatch.setattr(requests.Session, "head", lambda self, url, **kw: api.head(url, **kw))
    ctx.monkeypatch.setattr(requests.Session, "get", lambda self, url, **kw: api.get(url, **kw))


SETTINGS = SimpleNamespace(databricks_host="", databricks_token="")


@pytest.mark.asyncio
async def test_import_file_runs_shared_pipeline_with_user_token(app_mode):
    api = _FilesAPI(files={f"{ROOT}/guide.md": b"# Guide"})
    _use_files_api(app_mode, api)

    result = await routes.import_volume_documents(
        _Request({"paths": [f"{ROOT}/guide.md"]}), SimpleNamespace(), SETTINGS
    )

    assert result["success"] is True
    item = result["results"][0]
    assert item["filename"] == "guide.md"
    assert item["source_path"] == f"{ROOT}/guide.md"
    assert item["parse_status"] == "ready"
    assert app_mode.parse_service.prepared == [(FOLDER, VERSION, "guide.md", b"# Guide")]
    assert api.bearers and set(api.bearers) == {"Bearer USER_TOKEN"}


@pytest.mark.asyncio
async def test_import_folder_imports_supported_direct_children(app_mode):
    contents = [
        {"name": "a.md", "is_directory": False, "file_size": 2},
        {"name": "b.exe", "is_directory": False, "file_size": 2},
        {"name": "sub/", "is_directory": True},
    ]
    api = _FilesAPI(files={f"{ROOT}/a.md": b"aa"}, dirs={ROOT: contents})
    _use_files_api(app_mode, api)

    result = await routes.import_volume_documents(
        _Request({"paths": [ROOT]}), SimpleNamespace(), SETTINGS
    )

    assert [r["filename"] for r in result["results"]] == ["a.md"]
    assert api.downloads == [f"{ROOT}/a.md"]


@pytest.mark.asyncio
async def test_forbidden_file_is_reported_and_not_stored(app_mode):
    path = f"{ROOT}/secret.pdf"
    api = _FilesAPI(files={path: b"%PDF"}, forbidden={path})
    _use_files_api(app_mode, api)

    result = await routes.import_volume_documents(
        _Request({"paths": [path]}), SimpleNamespace(), SETTINGS
    )

    assert result["success"] is False
    assert "permission" in result["results"][0]["message"]
    assert app_mode.parse_service.prepared == []
    assert api.downloads == []


@pytest.mark.asyncio
async def test_missing_user_token_fails_closed_before_any_volume_call(app_mode):
    api = _FilesAPI(files={f"{ROOT}/a.md": b"a"})
    _use_files_api(app_mode, api)
    token = set_request_identity(RequestIdentity(email="u@x.com", user_token=""))
    try:
        with pytest.raises(AuthorizationError):
            await routes.import_volume_documents(
                _Request({"paths": [f"{ROOT}/a.md"]}), SimpleNamespace(), SETTINGS
            )
        with pytest.raises(AuthorizationError):
            await routes.browse_document_volume(ROOT, SimpleNamespace(), SETTINGS)
    finally:
        reset_request_identity(token)
    assert api.bearers == []
    assert app_mode.parse_service.prepared == []


@pytest.mark.asyncio
async def test_unsaved_domain_is_rejected(app_mode):
    app_mode.domain.uc_domain_folder = ""
    app_mode.domain.domain_folder = ""
    with pytest.raises(ValidationError):
        await routes.import_volume_documents(
            _Request({"paths": [f"{ROOT}/a.md"]}), SimpleNamespace(), SETTINGS
        )


@pytest.mark.asyncio
async def test_invalid_path_never_reaches_the_files_api(app_mode):
    api = _FilesAPI()
    _use_files_api(app_mode, api)
    result = await routes.import_volume_documents(
        _Request({"paths": ["/Volumes/main/docs/raw/../../other/x.md"]}),
        SimpleNamespace(),
        SETTINGS,
    )
    assert result["success"] is False
    assert api.bearers == []


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
