"""One Unity Catalog Volume file selected for Knowledge Store import."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VolumeDocument:
    """A resolved ``/Volumes/...`` file; ``error`` is set when it must be skipped."""

    path: str
    filename: str
    size: int | None = None
    error: str = ""
