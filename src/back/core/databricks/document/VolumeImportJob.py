"""Run a planned Unity Catalog Volume import in bounded-concurrency batches.

The job is executed on a background task, so the caller's token must already
be bound into ``source`` (captured at request time). Files are processed in
batches of ``BATCH_SIZE`` with at most ``CONCURRENCY`` downloads in flight.
A 401 means the short-lived forwarded token expired: the job stops, records
every file it did not get to as *remaining*, and never retries with another
identity.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from back.core.databricks.document.ParseStatus import ParseStatus
from back.core.databricks.document.VolumeDocument import VolumeDocument
from back.core.databricks.document.VolumeImportPlan import VolumeImportPlan
from back.core.errors import OntoBricksError, SessionExpiredError
from back.core.logging import get_logger

logger = get_logger(__name__)

Submit = Callable[[str, bytes], dict[str, Any]]
Progress = Callable[[dict[str, Any]], None]


class VolumeImportJob:
    """Download planned volume files as the caller and hand each to ``submit``."""

    BATCH_SIZE = 50
    CONCURRENCY = 4
    CANCELLED = "Import cancelled"

    def __init__(
        self,
        source: Any,
        submit: Submit,
        *,
        on_progress: Progress | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        batch_size: int = BATCH_SIZE,
        concurrency: int = CONCURRENCY,
    ) -> None:
        self._source = source
        self._submit = submit
        self._on_progress = on_progress or (lambda _summary: None)
        self._is_cancelled = is_cancelled or (lambda: False)
        self._batch_size = batch_size
        self._concurrency = concurrency
        self._stop = threading.Event()
        self._stop_reason = ""

    def run(self, plan: VolumeImportPlan) -> dict[str, Any]:
        """Process every planned document; return the final summary."""
        importable = [d for d in plan.docs if not d.error]
        batches = [
            importable[i : i + self._batch_size]
            for i in range(0, len(importable), self._batch_size)
        ]
        summary: dict[str, Any] = {
            "total": len(plan.docs),
            "processed": 0,
            "imported": 0,
            "failed": 0,
            "skipped_over_cap": plan.skipped_over_cap,
            "listing_truncated": plan.listing_truncated,
            "batches": len(batches),
            "batches_done": 0,
            "failures": [],
            "remaining": [],
            "stopped_reason": "",
        }
        for doc in plan.docs:
            if doc.error:
                self._record(summary, self._failed(doc, doc.error))
        self._emit(summary)

        for batch in batches:
            if self._should_stop():
                summary["remaining"].extend(d.path for d in batch)
                continue
            self._run_batch(batch, summary)
            summary["batches_done"] += 1
            self._emit(summary)

        summary["remaining"].sort()
        summary["stopped_reason"] = self._stop_reason
        return summary

    # -- batch / file ---------------------------------------------------

    def _run_batch(self, batch: list[VolumeDocument], summary: dict[str, Any]) -> None:
        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            futures = {pool.submit(self._import_one, doc): doc for doc in batch}
            for future in as_completed(futures):
                item = future.result()
                if item is None:
                    summary["remaining"].append(futures[future].path)
                else:
                    self._record(summary, item)
                self._emit(summary)

    def _import_one(self, doc: VolumeDocument) -> dict[str, Any] | None:
        """Return the result row, or ``None`` when the job stopped first."""
        if self._should_stop():
            return None
        try:
            content = self._source.read(doc)
        except SessionExpiredError as exc:
            self._halt(exc.message)
            return None
        except OntoBricksError as exc:
            return self._failed(doc, exc.message)
        except Exception as exc:  # noqa: BLE001 - reported per file
            logger.warning(
                "Volume read failed for %s: %s", doc.path, type(exc).__name__
            )
            return self._failed(doc, "Could not read the file from the volume")
        item = self._submit(doc.filename, content)
        logger.info("Knowledge Store import of %s from %s", doc.filename, doc.path)
        return {**item, "source_path": doc.path}

    # -- bookkeeping ----------------------------------------------------

    def _should_stop(self) -> bool:
        if not self._stop.is_set() and self._is_cancelled():
            self._halt(self.CANCELLED)
        return self._stop.is_set()

    def _halt(self, reason: str) -> None:
        if not self._stop.is_set():
            self._stop_reason = reason
            self._stop.set()
            logger.warning("Volume import stopped: %s", reason)

    @staticmethod
    def _failed(doc: VolumeDocument, message: str) -> dict[str, Any]:
        return {
            "filename": doc.filename,
            "source_path": doc.path,
            "success": False,
            "parse_status": ParseStatus.FAILED.value,
            "message": message,
        }

    @staticmethod
    def _record(summary: dict[str, Any], item: dict[str, Any]) -> None:
        summary["processed"] += 1
        if item.get("success") and item.get("parse_status") != ParseStatus.FAILED.value:
            summary["imported"] += 1
            return
        summary["failed"] += 1
        summary["failures"].append(
            {
                "filename": item.get("filename", ""),
                "source_path": item.get("source_path", ""),
                "message": item.get("message") or "Import failed",
            }
        )

    def _emit(self, summary: dict[str, Any]) -> None:
        snapshot = {
            **summary,
            "failures": list(summary["failures"]),
            "remaining": sorted(summary["remaining"]),
            "stopped_reason": self._stop_reason,
        }
        self._on_progress(snapshot)
