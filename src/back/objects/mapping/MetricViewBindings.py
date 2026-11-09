"""Resolve which mapped attributes read a Unity Catalog metric-view column."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from back.objects.mapping.Mapping import Mapping


class MetricViewBindings:
    """Derive ``derivedFrom`` targets from entity mappings and data-source metadata.

    Pure: mappings are only read, never modified. An attribute is bound when its
    entity mapping reads a metric view (``catalog``/``schema``/``table`` or the
    ``FROM`` of its SQL) and its column is one of that view's columns.

    A class may link several Unity Catalog datasets (tables, views, metric
    views). The mapping stays the source of truth -- a linked metric view the
    mapping does not read never creates a binding. When the mapping reads more
    than one metric view exposing the column, a view linked to the class wins
    over one that is not; otherwise the explicit ``table`` comes first, then
    the SQL sources in order.
    """

    @staticmethod
    def attribute_key(class_uri: str, attribute: str) -> str:
        """Stable key of an attribute reference (class URI + attribute name)."""
        return f"{class_uri}|{attribute}"

    @classmethod
    def resolve(
        cls,
        entity_mappings: Iterable[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]],
        previous: Optional[Dict[str, Dict[str, Any]]] = None,
        linked: Optional[Dict[str, Iterable[str]]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Return ``{attribute_key: {"view", "column", "role"}}`` for every binding.

        *previous* holds the last known targets: a view that vanished from the
        loaded metadata keeps its binding while the mapping still reads the same
        view and column, so a metadata reload never silently drops a reference.
        *linked* maps a class URI to the ``catalog.schema.name`` of its linked
        datasets; it only breaks ties between metric views the mapping reads.
        """
        views = cls._metric_views(metadata)
        previous = previous or {}
        linked = linked or {}
        bindings: Dict[str, Dict[str, Any]] = {}
        for ent in entity_mappings or []:
            class_uri = ent.get("ontology_class") or ent.get("class_uri") or ""
            if not class_uri or ent.get("excluded"):
                continue
            sources = cls._ordered_sources(ent, linked.get(class_uri) or ())
            excluded = set(ent.get("excluded_attributes") or [])
            for attr, column in (ent.get("attribute_mappings") or {}).items():
                if attr in excluded or not column:
                    continue
                key = cls.attribute_key(class_uri, attr)
                target = cls._bind(sources, str(column), views, previous.get(key))
                if target:
                    bindings[key] = target
        return bindings

    @staticmethod
    def _ordered_sources(ent: Dict[str, Any], linked: Iterable[str]) -> List[str]:
        """Distinct lower-cased sources of *ent*, class-linked datasets first."""
        sources: List[str] = []
        for triple in Mapping._entity_source_triples(ent):
            name = ".".join(triple).lower()
            if name not in sources:
                sources.append(name)
        linked_names = {str(n).lower() for n in linked}
        return [s for s in sources if s in linked_names] + [
            s for s in sources if s not in linked_names
        ]

    @staticmethod
    def _metric_views(metadata: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Index metric-view metadata entries by lower-cased full name."""
        return {
            str(t.get("full_name") or "").lower(): t
            for t in (metadata or {}).get("tables", []) or []
            if t.get("object_kind") == "metric_view" and t.get("full_name")
        }

    @staticmethod
    def _bind(
        sources: List[str],
        column: str,
        views: Dict[str, Dict[str, Any]],
        previous: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        for source in sources:
            entry = views.get(source)
            if entry is None:
                continue
            for col in entry.get("columns", []) or []:
                if str(col.get("name", "")).lower() == column.lower():
                    return {
                        "view": entry["full_name"],
                        "column": col["name"],
                        "role": col.get("role") or "dimension",
                    }
        if (
            previous
            and str(previous.get("view", "")).lower() in sources
            and str(previous.get("view", "")).lower() not in views
            and str(previous.get("column", "")).lower() == column.lower()
        ):
            return dict(previous)
        return None
