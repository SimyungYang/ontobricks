"""Knowledge Store documents read from Unity Catalog Volumes as the caller.

The Files API client handed to this class must be bound to the end user's
token (on-behalf-of) so Unity Catalog decides what can be listed and read; a
user can never import a file they could not open themselves. Size and type
are checked from metadata before any download, and the download itself is
streamed with the same cap as a local upload.
"""

from __future__ import annotations

import posixpath
from typing import Any

from back.core.databricks.document.DocumentExtractor import DocumentExtractor
from back.core.databricks.document.DocumentParseService import DocumentParseService
from back.core.databricks.document.VolumeDocument import VolumeDocument
from back.core.errors import (
    AuthorizationError,
    InfrastructureError,
    NotFoundError,
    OntoBricksError,
    ValidationError,
)
from back.core.logging import get_logger

logger = get_logger(__name__)


class VolumeDocumentSource:
    """Browse, resolve and read ``/Volumes/...`` files for Knowledge Store import."""

    PATH_PREFIX = "/Volumes/"
    MAX_PATH_LENGTH = 1024
    MAX_IMPORT_PATHS = 50
    MAX_IMPORT_FILES = 50
    MAX_BROWSE_ENTRIES = 500
    SCOPE_HINT = (
        "If you can open it in Databricks, the app may be missing the "
        "'files.files' user authorization scope."
    )

    def __init__(
        self, files: Any, max_bytes: int = DocumentParseService.MAX_UPLOAD_BYTES
    ) -> None:
        self._files = files
        self._max_bytes = max_bytes

    # -- validation -------------------------------------------------

    @classmethod
    def normalize_path(cls, path: str) -> str:
        """Return a canonical ``/Volumes/<catalog>/<schema>/<volume>[/...]`` path."""
        candidate = str(path or "").strip()
        if len(candidate) > len(cls.PATH_PREFIX):
            candidate = candidate.rstrip("/")
        bad_char = any(ord(ch) < 32 or ord(ch) == 127 for ch in candidate)
        segments = candidate[len(cls.PATH_PREFIX):].split("/")
        if (
            not candidate.startswith(cls.PATH_PREFIX)
            or len(candidate) > cls.MAX_PATH_LENGTH
            or "\\" in candidate
            or bad_char
            or len(segments) < 3
            or any(seg in ("", ".", "..") for seg in segments)
        ):
            raise ValidationError(
                "Path must look like /Volumes/<catalog>/<schema>/<volume>/..."
            )
        return candidate

    @staticmethod
    def is_supported(filename: str) -> bool:
        """True when the Knowledge Store can parse this file type."""
        extension = DocumentExtractor.file_extension(filename)
        return (
            extension in DocumentParseService.TEXT_EXTENSIONS
            or DocumentExtractor.supports(extension)
        )

    def _limit_message(self) -> str:
        return f"File exceeds the {self._max_bytes // (1024 * 1024)} MB upload limit"

    @classmethod
    def _status_error(cls, status: int, path: str) -> OntoBricksError:
        if status in (401, 403):
            return AuthorizationError(
                f"You do not have permission to read {path}. {cls.SCOPE_HINT}"
            )
        if status == 404:
            return NotFoundError(f"Path not found: {path}")
        return InfrastructureError(
            f"Unity Catalog Files API returned HTTP {status} for {path}"
        )

    # -- browse -----------------------------------------------------

    def browse(self, path: str) -> dict[str, Any]:
        """List one folder's direct children, flagging importable files."""
        path = self.normalize_path(path)
        status, items, truncated = self._files.list_directory_page(
            path, self.MAX_BROWSE_ENTRIES
        )
        if status != 200:
            raise self._status_error(status, path)
        entries = [
            {
                **item,
                "supported": item["is_directory"] or self.is_supported(item["name"]),
                "too_large": (not item["is_directory"])
                and int(item.get("size") or 0) > self._max_bytes,
            }
            for item in items
        ]
        entries.sort(key=lambda e: (not e["is_directory"], e["name"].lower()))
        return {"path": path, "items": entries, "truncated": truncated}

    # -- import plan ------------------------------------------------

    def plan(self, paths: Any) -> tuple[list[VolumeDocument], bool]:
        """Resolve file/folder paths into documents; return ``(docs, truncated)``.

        Folders contribute the supported files directly inside them (not
        recursive). At most ``MAX_IMPORT_FILES`` files are accepted per call;
        duplicate paths are dropped and a second file with an already-used
        name is reported instead of silently overwriting the first.
        """
        if not isinstance(paths, list) or not [p for p in paths if str(p).strip()]:
            raise ValidationError("At least one /Volumes path is required")
        if len(paths) > self.MAX_IMPORT_PATHS:
            raise ValidationError(
                f"At most {self.MAX_IMPORT_PATHS} paths can be imported at once"
            )
        docs: list[VolumeDocument] = []
        seen_paths: set[str] = set()
        seen_names: set[str] = set()
        truncated = False
        for raw in (str(p).strip() for p in paths if str(p).strip()):
            resolved, folder_truncated = self._resolve(raw)
            truncated = truncated or folder_truncated
            for doc in resolved:
                if doc.path in seen_paths:
                    continue
                seen_paths.add(doc.path)
                if not doc.error and doc.filename in seen_names:
                    doc = VolumeDocument(
                        doc.path,
                        doc.filename,
                        doc.size,
                        "Another selected file has the same name",
                    )
                if not doc.error:
                    if len(seen_names) >= self.MAX_IMPORT_FILES:
                        truncated = True
                        continue
                    seen_names.add(doc.filename)
                docs.append(doc)
        return docs, truncated

    def _resolve(self, raw: str) -> tuple[list[VolumeDocument], bool]:
        """Resolve one user path as a file, else as a folder."""
        try:
            path = self.normalize_path(raw)
        except ValidationError as exc:
            return [VolumeDocument(raw, posixpath.basename(raw), error=exc.message)], False
        try:
            file_status, size = self._files.head_file(path)
            if file_status == 200:
                return [self._checked(path, size)], False
            if file_status in (401, 403):
                return [self._failed(path, file_status)], False
            # Not a readable file (a folder answers 404 or 4xx here): try a listing.
            status, items, truncated = self._files.list_directory_page(
                path, self.MAX_BROWSE_ENTRIES
            )
        except Exception as exc:  # noqa: BLE001 - reported per path, never fatal
            logger.warning(
                "Files API call failed for %s: %s", path, type(exc).__name__
            )
            return [
                VolumeDocument(
                    path,
                    posixpath.basename(path),
                    error="Could not reach the Unity Catalog Files API",
                )
            ], False
        if status != 200:
            return [self._failed(path, file_status if status == 404 else status)], False
        files = sorted(
            (i for i in items if not i["is_directory"] and self.is_supported(i["name"])),
            key=lambda i: i["name"].lower(),
        )
        if not files:
            return [
                VolumeDocument(
                    path,
                    posixpath.basename(path),
                    error="No supported documents directly inside this folder",
                )
            ], truncated
        return [self._checked(i["path"], i.get("size")) for i in files], truncated

    def _checked(self, path: str, size: int | None) -> VolumeDocument:
        filename = posixpath.basename(path)
        if not self.is_supported(filename):
            return VolumeDocument(path, filename, size, "Unsupported document type")
        if size is not None and size > self._max_bytes:
            return VolumeDocument(path, filename, size, self._limit_message())
        return VolumeDocument(path, filename, size)

    def _failed(self, path: str, status: int) -> VolumeDocument:
        return VolumeDocument(
            path,
            posixpath.basename(path),
            error=self._status_error(status, path).message,
        )

    # -- read -------------------------------------------------------

    def read(self, doc: VolumeDocument) -> bytes:
        """Download one planned document, aborting past the size cap."""
        status, data = self._files.read_binary_file_capped(doc.path, self._max_bytes)
        if status != 200:
            raise self._status_error(status, doc.path)
        if data is None:
            raise ValidationError(self._limit_message())
        return data
