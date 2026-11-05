"""Pull referenced definitions from Unity Catalog into the ontology (never writes UC)."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from back.core.databricks.uc.GlossaryConnector import GlossaryConnector
from back.core.databricks.uc.NotAvailableGlossaryConnector import NotAvailableGlossaryConnector
from back.core.errors import OntoBricksError
from back.core.logging import get_logger
from back.objects.ontology.definitions.DefinitionReferences import DefinitionReferences

logger = get_logger(__name__)

STATUS_UPDATED = "updated"
STATUS_UNCHANGED = "unchanged"
STATUS_WARNING = "warning"


class _LazyMetricSource:
    """Build the metric source on first use so a sync without views does no I/O."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._source: Any = None

    def get_metric_view_definition(self, catalog: str, schema: str, name: str) -> Any:
        if self._source is None:
            self._source = self._factory()
        return self._source.get_metric_view_definition(catalog, schema, name)


class DefinitionSyncService:
    """Refresh metric-view and UC Page definitions and report the drift.

    *metric_source* is anything with
    ``get_metric_view_definition(catalog, schema, name)`` (a
    :class:`~back.core.databricks.DatabricksClient`). A source that cannot be
    read, or a target that disappeared, keeps the last synced value and sets a
    warning on the item -- nothing is ever dropped silently.
    """

    def __init__(
        self,
        metric_source: Any,
        glossary: Optional[GlossaryConnector] = None,
        clock: Optional[Callable[[], str]] = None,
    ) -> None:
        self._metric_source = metric_source
        self._glossary = glossary or NotAvailableGlossaryConnector()
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    @classmethod
    def sync_session(cls, domain: Any, client_factory: Callable[[], Any]) -> Dict[str, Any]:
        """Sync a session domain in place; invalidate generated OWL when it changed.

        *client_factory* builds the metric source (a ``DatabricksClient``) and is
        only called when a metric view must actually be read. The caller owns
        ``domain.save()``. Returns the drift report (``{}`` without references).
        """
        ontology = getattr(domain, "ontology", None)
        if not isinstance(ontology, dict):
            return {}
        mappings = (getattr(domain, "assignment", None) or {}).get("entities", [])
        metadata = getattr(domain, "catalog_metadata", None)
        report, changed = cls(_LazyMetricSource(client_factory)).sync(ontology, mappings, metadata)
        if changed and hasattr(domain, "clear_generated_content"):
            domain.clear_generated_content()
        return report

    def sync(
        self,
        ontology: Dict[str, Any],
        entity_mappings: Optional[List[Dict[str, Any]]],
        metadata: Optional[Dict[str, Any]],
    ) -> Tuple[Dict[str, Any], bool]:
        """Reconcile references, pull UC content, return ``(report, changed)``."""
        changed = DefinitionReferences.reconcile(ontology, entity_mappings, metadata)
        if not DefinitionReferences.has_items(ontology):
            return {}, changed
        items = DefinitionReferences.items(ontology)
        before = copy.deepcopy(items)
        views = self._fetch_metric_views(items)
        glossary_available, terms = self._fetch_glossary(items)
        now = self._clock()
        entries = [
            self._sync_item(key, item, views, glossary_available, terms, now)
            for key, item in sorted(items.items())
        ]
        changed |= before != items  # content or warning state moved
        changed |= DefinitionReferences.apply_to_classes(ontology)
        report = self._report(entries, glossary_available, now)
        DefinitionReferences.set_last_report(ontology, report)
        logger.info("Definition sync: %s", report["message"])
        return report, changed

    # ------------------------------------------------------------------
    # Fetching (read-only)
    # ------------------------------------------------------------------

    def _fetch_metric_views(self, items: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Return ``{view_fqn: column definitions | None (unreadable)}``."""
        views: Dict[str, Any] = {}
        for item in items.values():
            view = (item.get("derived_from") or {}).get("view")
            if not view or view in views:
                continue
            parts = view.split(".")
            try:
                if len(parts) != 3:
                    raise ValueError("metric view name is not catalog.schema.view")
                views[view] = self._metric_source.get_metric_view_definition(*parts)
            except (OntoBricksError, ValueError) as exc:
                logger.warning("Definition sync: metric view %s unreadable: %s", view, exc)
                views[view] = None
        return views

    def _fetch_glossary(
        self, items: Dict[str, Dict[str, Any]]
    ) -> Tuple[bool, Dict[str, Optional[Dict[str, Any]]]]:
        refs = sorted({i["glossary_ref"] for i in items.values() if i.get("glossary_ref")})
        if not refs:
            return True, {}
        try:
            if not self._glossary.is_available():
                return False, {}
            return True, self._glossary.fetch_terms(refs)
        except OntoBricksError as exc:
            logger.info("Definition sync: UC Pages lookup failed (%s): %s", self._glossary.name, exc)
            return False, {}

    # ------------------------------------------------------------------
    # Per-item sync
    # ------------------------------------------------------------------

    def _sync_item(
        self,
        key: str,
        item: Dict[str, Any],
        views: Dict[str, Any],
        glossary_available: bool,
        terms: Dict[str, Optional[Dict[str, Any]]],
        now: str,
    ) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "key": key,
            "kind": "attribute" if item.get("attribute") else "class",
            "class_uri": item.get("class_uri", ""),
            "attribute": item.get("attribute", ""),
            "changes": {},
            "warnings": [],
        }
        synced = item.setdefault("synced", {})
        if item.get("derived_from"):
            self._sync_metric(item["derived_from"], views, synced, entry, now)
        if item.get("glossary_ref"):
            self._sync_page(item["glossary_ref"], glossary_available, terms, synced, entry, now)
        if not synced:
            item.pop("synced")
        DefinitionReferences.set_warning(item, "; ".join(w["message"] for w in entry["warnings"]))
        if entry["changes"]:
            entry["status"] = STATUS_UPDATED
        else:
            entry["status"] = STATUS_WARNING if entry["warnings"] else STATUS_UNCHANGED
        return entry

    def _sync_metric(
        self,
        target: Dict[str, Any],
        views: Dict[str, Any],
        synced: Dict[str, Any],
        entry: Dict[str, Any],
        now: str,
    ) -> None:
        view, column = target.get("view", ""), target.get("column", "")
        entry["target"] = f"{view}#{column}"
        columns = views.get(view)
        if columns is None:
            self._warn(entry, "source_unavailable", f"Metric view {view} could not be read")
            return
        definition = columns.get(column) or next(
            (d for name, d in columns.items() if name.lower() == column.lower()), None
        )
        if definition is None:
            self._warn(entry, "target_missing", f"{column} no longer exists in {view}")
            return
        fresh = {
            "expr": definition.get("expr", ""),
            "comment": definition.get("comment", ""),
            "synonyms": list(definition.get("synonyms") or []),
            "role": definition.get("role", target.get("role", "")),
            "source": f"uc_metric_view:{view}",
        }
        self._store(synced, "metric_view", fresh, entry, now, ("expr", "comment"))

    def _sync_page(
        self,
        ref: str,
        available: bool,
        terms: Dict[str, Optional[Dict[str, Any]]],
        synced: Dict[str, Any],
        entry: Dict[str, Any],
        now: str,
    ) -> None:
        entry["glossary_ref"] = ref
        if not available:
            self._warn(entry, "source_unavailable", "UC Pages API not available")
            return
        term = terms.get(ref)
        if term is None:
            self._warn(entry, "target_missing", f"UC Page {ref} no longer exists")
            return
        fresh = {
            "definition": str(term.get("definition") or ""),
            "synonyms": [str(s) for s in term.get("synonyms") or [] if s],
            "url": str(term.get("url") or ""),
            "source": f"uc_page:{ref}",
        }
        self._store(synced, "page", fresh, entry, now, ("definition",))

    @staticmethod
    def _store(
        synced: Dict[str, Any],
        source: str,
        fresh: Dict[str, Any],
        entry: Dict[str, Any],
        now: str,
        text_fields: Tuple[str, ...],
    ) -> None:
        """Record *fresh* under *source* and the before/after drift into *entry*."""
        previous = synced.get(source) or {}
        if previous and all(previous.get(k) == v for k, v in fresh.items()):
            return
        for field in text_fields:
            if previous.get(field, "") != fresh[field]:
                entry["changes"][field] = {"before": previous.get(field, ""), "after": fresh[field]}
        old = set(previous.get("synonyms") or [])
        new = set(fresh["synonyms"])
        if new - old:
            entry["changes"]["synonyms_added"] = sorted(new - old)
        if old - new:
            entry["changes"]["synonyms_removed"] = sorted(old - new)
        if not entry["changes"]:
            entry["changes"]["metadata"] = {"source": source}
        synced[source] = {**fresh, "synced_at": now}

    @staticmethod
    def _warn(entry: Dict[str, Any], reason: str, message: str) -> None:
        entry["warnings"].append({"reason": reason, "message": message})

    def _report(
        self, entries: List[Dict[str, Any]], glossary_available: bool, now: str
    ) -> Dict[str, Any]:
        updated = sum(1 for e in entries if e["status"] == STATUS_UPDATED)
        warned = sum(1 for e in entries if e["warnings"])
        return {
            "checked_at": now,
            "summary": {
                "checked": len(entries),
                "updated": updated,
                "unchanged": sum(1 for e in entries if e["status"] == STATUS_UNCHANGED),
                "warnings": warned,
            },
            "message": f"{updated} definitions updated, {warned} warnings",
            "glossary_connector": {
                "name": self._glossary.name,
                "available": glossary_available,
            },
            "entries": entries,
        }
