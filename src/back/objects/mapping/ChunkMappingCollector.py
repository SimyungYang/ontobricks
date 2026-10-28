"""Keep an auto-assign chunk's agent mappings that target the chunk's own items."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Set

from back.core.logging import get_logger
from back.core.w3c.rdf_utils import uri_local_name

logger = get_logger(__name__)


class ChunkMappingCollector:
    """Match agent mappings to chunk items, repairing a mistyped URI by its local name.

    The agent copies each item's URI into its submit call and sometimes
    mistypes the host (e.g. ``databricks-ontobricks.com`` for
    ``databricks-ontology.com``). The submit tool accepts any URI, so an
    exact-match filter silently dropped those mappings. A mapping whose URI
    is not in the chunk is kept when its local name points to exactly one
    chunk item; it is then rewritten to that item's canonical URI.
    """

    @staticmethod
    def collect(
        mappings: Iterable[Dict[str, Any]],
        uri_keys: Sequence[str],
        chunk_uris: Set[str],
        into: Dict[str, Dict[str, Any]],
    ) -> int:
        """Upsert chunk mappings into *into* (keyed by canonical URI); return how many were kept."""
        by_local: Dict[str, List[str]] = {}
        for uri in chunk_uris:
            by_local.setdefault(uri_local_name(uri), []).append(uri)

        kept = 0
        for mapping in mappings:
            uri = next((mapping[k] for k in uri_keys if mapping.get(k)), "")
            if not uri:
                continue
            if uri not in chunk_uris:
                candidates = by_local.get(uri_local_name(uri), [])
                if len(candidates) != 1:
                    continue
                logger.warning(
                    "Auto-assign: agent URI %s does not match the ontology; using %s",
                    uri,
                    candidates[0],
                )
                uri = candidates[0]
                mapping = {**mapping, **{k: uri for k in uri_keys if k in mapping}}
            into[uri] = mapping
            kept += 1
        return kept
