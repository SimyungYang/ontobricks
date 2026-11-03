"""Resolved file list for one Unity Catalog Volume import job."""

from __future__ import annotations

from dataclasses import dataclass, field

from back.core.databricks.document.VolumeDocument import VolumeDocument


@dataclass(frozen=True)
class VolumeImportPlan:
    """Documents to import (skipped ones carry ``error``) plus cap accounting.

    ``skipped_over_cap`` counts supported files left out because the job
    already holds the maximum; ``listing_truncated`` means a folder had more
    entries than the scan limit, so that count is a lower bound.
    """

    docs: list[VolumeDocument] = field(default_factory=list)
    skipped_over_cap: int = 0
    listing_truncated: bool = False
