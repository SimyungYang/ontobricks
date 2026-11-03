"""Tests for back.core.databricks.document.VolumeImportJob."""

from __future__ import annotations

import threading
import time

import pytest

from back.core.databricks import VolumeDocument, VolumeDocumentSource, VolumeImportJob
from back.core.databricks.document import VolumeImportPlan
from back.core.errors import AuthorizationError, SessionExpiredError

pytestmark = pytest.mark.unit

ROOT = "/Volumes/main/docs/raw"


def _docs(n: int) -> list[VolumeDocument]:
    return [VolumeDocument(f"{ROOT}/doc{i:03d}.md", f"doc{i:03d}.md", 4) for i in range(n)]


class _Source:
    """Records reads; ``fail`` maps path -> exception raised on read."""

    def __init__(self, fail=None, delay=0.0):
        self.fail = fail or {}
        self.delay = delay
        self.reads: list[str] = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def read(self, doc):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.reads.append(doc.path)
        try:
            if self.delay:
                time.sleep(self.delay)
            if doc.path in self.fail:
                raise self.fail[doc.path]
            return doc.filename.encode()
        finally:
            with self.lock:
                self.active -= 1


class _Submit:
    def __init__(self):
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, filename, content):
        with self.lock:
            self.calls.append(filename)
        return {"filename": filename, "success": True, "parse_status": "ready",
                "message": "Parsed and ready"}


def test_120_files_run_in_3_batches_and_all_import():
    source, submit, progress = _Source(), _Submit(), []
    job = VolumeImportJob(source, submit, on_progress=progress.append)
    summary = job.run(VolumeImportPlan(docs=_docs(120)))
    assert summary["batches"] == 3
    assert summary["batches_done"] == 3
    assert summary["imported"] == 120
    assert summary["failed"] == 0
    assert summary["remaining"] == []
    assert sorted(submit.calls) == [f"doc{i:03d}.md" for i in range(120)]
    assert progress[-1]["processed"] == 120
    # Progress is published as files finish, not only at the end.
    assert len(progress) > 3


def test_600_files_import_500_and_report_100_skipped():
    children = [
        {"name": f"doc{i:03d}.md", "path": f"{ROOT}/doc{i:03d}.md",
         "is_directory": False, "size": 4}
        for i in range(600)
    ]

    class _Files:
        def head_file(self, path):
            return 404, None

        def list_directory_page(self, path, max_entries):
            return 200, children[:max_entries], len(children) > max_entries

    plan = VolumeDocumentSource(_Files()).plan([ROOT])
    summary = VolumeImportJob(_Source(), _Submit()).run(plan)
    assert summary["imported"] == 500
    assert summary["skipped_over_cap"] == 100
    assert summary["batches"] == 10


def test_concurrency_is_bounded_and_batches_do_not_overlap():
    source = _Source(delay=0.01)
    batch_of: dict[str, int] = {}
    docs = _docs(20)
    for i, doc in enumerate(docs):
        batch_of[doc.path] = i // 8
    job = VolumeImportJob(source, _Submit(), batch_size=8, concurrency=3)
    job.run(VolumeImportPlan(docs=docs))
    assert 1 < source.max_active <= 3
    # Reads arrive batch by batch: the batch index never goes backwards.
    order = [batch_of[p] for p in source.reads]
    assert order == sorted(order)


def test_default_batch_and_concurrency():
    assert VolumeImportJob.BATCH_SIZE == 50
    assert VolumeImportJob.CONCURRENCY == 4


def test_expired_token_mid_job_stops_and_reports_remaining_files():
    docs = _docs(120)
    expired = docs[60].path  # second batch
    source = _Source(fail={expired: SessionExpiredError("sign-in expired")})
    submit = _Submit()
    summary = VolumeImportJob(source, submit, concurrency=1).run(
        VolumeImportPlan(docs=docs)
    )
    assert summary["stopped_reason"] == "sign-in expired"
    assert summary["imported"] == 60
    assert summary["failed"] == 0
    assert summary["remaining"] == sorted(d.path for d in docs[60:])
    # Nothing after the expired file was read; the third batch never started.
    assert source.reads[-1] == expired
    assert len(submit.calls) == 60


def test_forbidden_file_fails_alone_and_the_job_continues():
    docs = _docs(5)
    source = _Source(fail={docs[2].path: AuthorizationError("You do not have permission")})
    summary = VolumeImportJob(source, _Submit()).run(VolumeImportPlan(docs=docs))
    assert summary["imported"] == 4
    assert summary["failed"] == 1
    assert summary["failures"][0]["source_path"] == docs[2].path
    assert summary["stopped_reason"] == ""


def test_planned_errors_and_parse_failures_are_reported_with_reasons():
    skipped = VolumeDocument(f"{ROOT}/x.exe", "x.exe", 1, "Unsupported document type")

    def submit(filename, content):
        return {"filename": filename, "success": True, "parse_status": "failed",
                "message": "Document parsing failed"}

    summary = VolumeImportJob(_Source(), submit).run(
        VolumeImportPlan(docs=[skipped, *_docs(1)])
    )
    assert summary["total"] == 2
    assert summary["failed"] == 2
    assert {f["message"] for f in summary["failures"]} == {
        "Unsupported document type",
        "Document parsing failed",
    }


def test_cancellation_stops_before_the_next_file():
    source = _Source()
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return len(source.reads) >= 3

    summary = VolumeImportJob(source, _Submit(), concurrency=1, is_cancelled=cancelled).run(
        VolumeImportPlan(docs=_docs(10))
    )
    assert summary["stopped_reason"] == VolumeImportJob.CANCELLED
    assert summary["imported"] == 3
    assert len(summary["remaining"]) == 7
