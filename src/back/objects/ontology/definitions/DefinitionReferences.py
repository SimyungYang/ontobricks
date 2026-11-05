"""UC definition references stored on the ontology (``ontology["definition_refs"]``).

Layout::

    {"items": {key: {"class_uri", "attribute",          # "" for a class item
                     "derived_from": {"view", "column", "role"},   # optional
                     "glossary_ref": "<UC Page id or URL>",        # optional
                     "synced": {"metric_view": {...}, "page": {...}},
                     "warning": "<last sync warning>"}},           # optional
     "last_report": {...}}                                         # drift report

Only references live in the ontology; the content under ``synced`` is the last
copy pulled from Unity Catalog and is refreshed at every build / metadata
refresh. References are kept apart from ``classes[].dataProperties`` because
the entity editor rebuilds attribute dicts from their names on every save.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from back.core.errors import NotFoundError, ValidationError
from back.core.logging import get_logger

logger = get_logger(__name__)

STORE_KEY = "definition_refs"
_MAX_REF_LENGTH = 2048


class DefinitionReferences:
    """Read and maintain the UC definition references of an ontology dict."""

    # ------------------------------------------------------------------
    # Store access
    # ------------------------------------------------------------------

    @staticmethod
    def item_key(class_uri: str, attribute: str = "") -> str:
        """Key of a class item (its URI) or attribute item (``<class_uri>|<attr>``)."""
        return f"{class_uri}|{attribute}" if attribute else class_uri

    @staticmethod
    def items(ontology: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Return the mutable item map (created on demand)."""
        store = ontology.setdefault(STORE_KEY, {})
        return store.setdefault("items", {})

    @staticmethod
    def has_items(ontology: Dict[str, Any]) -> bool:
        return bool(((ontology or {}).get(STORE_KEY) or {}).get("items"))

    @staticmethod
    def last_report(ontology: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Return the drift report of the last sync, or ``{}``."""
        return dict(((ontology or {}).get(STORE_KEY) or {}).get("last_report") or {})

    @staticmethod
    def set_last_report(ontology: Dict[str, Any], report: Dict[str, Any]) -> None:
        store = ontology.setdefault(STORE_KEY, {})
        if report:
            store["last_report"] = report
        else:
            store.pop("last_report", None)

    @staticmethod
    def effective(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Merge the per-source synced content into the values the ontology shows.

        A UC Page definition wins over the metric-view comment; synonyms from
        every source are united. Returns ``None`` before the first sync.
        """
        synced = item.get("synced") or {}
        metric, page = synced.get("metric_view") or {}, synced.get("page") or {}
        if not metric and not page:
            return None
        alt_labels: List[str] = []
        for label in (metric.get("synonyms") or []) + (page.get("synonyms") or []):
            if label and label not in alt_labels:
                alt_labels.append(label)
        stamps = [s["synced_at"] for s in (metric, page) if s.get("synced_at")]
        return {
            "comment": page.get("definition") or metric.get("comment") or "",
            "alt_labels": alt_labels,
            "expr": metric.get("expr") or "",
            "synced_at": max(stamps) if stamps else "",
            "sources": [s["source"] for s in (metric, page) if s.get("source")],
        }

    # ------------------------------------------------------------------
    # Manual glossary references (entity / attribute edit API)
    # ------------------------------------------------------------------

    @classmethod
    def apply_glossary_updates(
        cls, ontology: Dict[str, Any], class_dict: Dict[str, Any], data: Dict[str, Any]
    ) -> bool:
        """Apply ``glossary_ref`` / ``attribute_glossary_refs`` from an update payload.

        The whole payload is validated before anything changes; an empty string
        clears a reference. Returns ``True`` when anything changed.
        """
        class_uri = class_dict.get("uri") or ""
        updates: List[tuple] = []
        if "glossary_ref" in data:
            updates.append(("", cls._clean_ref(data["glossary_ref"])))
        attr_refs = data.get("attribute_glossary_refs")
        if attr_refs is not None:
            if not isinstance(attr_refs, dict):
                raise ValidationError("attribute_glossary_refs must be an object")
            known = {cls._attr_name(dp) for dp in class_dict.get("dataProperties", []) or []}
            for attribute, ref in attr_refs.items():
                if attribute not in known:
                    raise NotFoundError(f"Attribute {attribute!r} not found on this entity")
                updates.append((attribute, cls._clean_ref(ref)))
        if updates and not class_uri:
            raise ValidationError("A class URI is required to set a glossary reference")
        changed = False
        for attribute, ref in updates:
            changed |= cls.set_glossary_ref(ontology, class_uri, attribute, ref)
        return changed

    @staticmethod
    def _clean_ref(ref: Any) -> str:
        value = str(ref or "").strip()
        if len(value) > _MAX_REF_LENGTH:
            raise ValidationError("Glossary reference is too long")
        return value

    @classmethod
    def set_glossary_ref(
        cls, ontology: Dict[str, Any], class_uri: str, attribute: str, ref: Any
    ) -> bool:
        """Set (or clear with ``""``/``None``) the UC Page reference of one item."""
        if not class_uri:
            raise ValidationError("A class URI is required to set a glossary reference")
        value = cls._clean_ref(ref)
        items = cls.items(ontology)
        key = cls.item_key(class_uri, attribute)
        item = items.get(key)
        if not value:
            if not item or "glossary_ref" not in item:
                return False
            item.pop("glossary_ref")
            (item.get("synced") or {}).pop("page", None)
            cls._drop_if_unreferenced(items, key)
            return True
        if item and item.get("glossary_ref") == value:
            return False
        item = items.setdefault(key, {"class_uri": class_uri, "attribute": attribute})
        item["glossary_ref"] = value
        (item.get("synced") or {}).pop("page", None)
        return True

    # ------------------------------------------------------------------
    # Reconciliation with mappings / classes
    # ------------------------------------------------------------------

    @classmethod
    def reconcile(
        cls,
        ontology: Dict[str, Any],
        entity_mappings: Optional[List[Dict[str, Any]]],
        metadata: Optional[Dict[str, Any]],
    ) -> bool:
        """Refresh ``derived_from`` from mappings, prune orphans, re-apply synced text.

        Mappings are only read. Returns ``True`` when the ontology changed.
        """
        from back.objects.mapping import MetricViewBindings

        if not entity_mappings and not cls.has_items(ontology):
            return False
        before = copy.deepcopy(ontology.get(STORE_KEY) or {})
        items = cls.items(ontology)
        previous = {k: i["derived_from"] for k, i in items.items() if i.get("derived_from")}
        bindings = MetricViewBindings.resolve(entity_mappings or [], metadata, previous)
        for key, item in list(items.items()):
            if item.get("attribute") and key not in bindings and item.pop("derived_from", None):
                (item.get("synced") or {}).pop("metric_view", None)
                cls._drop_if_unreferenced(items, key)
        for key, target in bindings.items():
            class_uri, attribute = key.split("|", 1)
            item = items.setdefault(key, {"class_uri": class_uri, "attribute": attribute})
            if item.get("derived_from") != target:
                item["derived_from"] = target
                (item.get("synced") or {}).pop("metric_view", None)
        cls._prune_missing_classes(ontology, items)
        classes_changed = cls.apply_to_classes(ontology)
        if not items and not ontology[STORE_KEY].get("last_report"):
            ontology.pop(STORE_KEY, None)
        return classes_changed or before != (ontology.get(STORE_KEY) or {})

    @classmethod
    def apply_to_classes(cls, ontology: Dict[str, Any]) -> bool:
        """Mirror synced UC text onto referenced classes (UC is the source of truth)."""
        items = ((ontology.get(STORE_KEY) or {}).get("items")) or {}
        changed = False
        for cls_dict in ontology.get("classes", []) or []:
            item = items.get(cls_dict.get("uri") or "")
            values = cls.effective(item) if item else None
            if values is None:
                continue
            fields = {"description": values["comment"], "alternate_labels": values["alt_labels"]}
            if "comment" in cls_dict:
                fields["comment"] = values["comment"]
            for field, value in fields.items():
                if cls_dict.get(field) != value:
                    cls_dict[field] = value
                    changed = True
        return changed

    @classmethod
    def adopt_synced(cls, ontology: Dict[str, Any], incoming: Optional[Dict[str, Any]]) -> bool:
        """Adopt the synced content computed on a snapshot (async metadata refresh).

        Only content for references that still point at the same target is
        taken; manual glossary references are never created from *incoming*.
        """
        if not isinstance(incoming, dict):
            return False
        items = cls.items(ontology)
        for key, inc in (incoming.get("items") or {}).items():
            live = items.get(key)
            if live is None:
                if not inc.get("derived_from"):
                    continue
                live = items[key] = {
                    "class_uri": inc.get("class_uri", ""),
                    "attribute": inc.get("attribute", ""),
                    "derived_from": inc["derived_from"],
                }
            if (live.get("derived_from"), live.get("glossary_ref")) != (
                inc.get("derived_from"),
                inc.get("glossary_ref"),
            ):
                continue
            live["synced"] = copy.deepcopy(inc.get("synced") or {})
            cls.set_warning(live, inc.get("warning", ""))
        cls.set_last_report(ontology, incoming.get("last_report") or {})
        cls.apply_to_classes(ontology)
        return True

    @classmethod
    def owl_annotations(cls, ontology: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Items flattened for :class:`OntologyDefinitionAnnotations` (OWL output)."""
        items = (((ontology or {}).get(STORE_KEY) or {}).get("items")) or {}
        return [{**item, "effective": cls.effective(item)} for _, item in sorted(items.items())]

    @classmethod
    def describe(cls, ontology: Dict[str, Any]) -> Dict[str, Any]:
        """API view: every reference with its effective synced values."""
        items = ((ontology.get(STORE_KEY) or {}).get("items")) or {}
        return {
            "items": [
                {"key": key, **copy.deepcopy(item), "effective": cls.effective(item)}
                for key, item in sorted(items.items())
            ],
            "last_report": cls.last_report(ontology),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def set_warning(item: Dict[str, Any], warning: str) -> None:
        if warning:
            item["warning"] = warning
        else:
            item.pop("warning", None)

    @staticmethod
    def _attr_name(data_prop: Any) -> str:
        if isinstance(data_prop, str):
            return data_prop
        return (data_prop or {}).get("name") or (data_prop or {}).get("localName") or ""

    @staticmethod
    def _drop_if_unreferenced(items: Dict[str, Dict[str, Any]], key: str) -> None:
        item = items.get(key) or {}
        if not item.get("derived_from") and not item.get("glossary_ref"):
            items.pop(key, None)

    @staticmethod
    def _prune_missing_classes(ontology: Dict[str, Any], items: Dict[str, Dict[str, Any]]) -> None:
        class_uris = {c.get("uri") for c in ontology.get("classes", []) or [] if c.get("uri")}
        for key, item in list(items.items()):
            if item.get("class_uri") not in class_uris:
                logger.info("Dropping definition reference %s: entity no longer exists", key)
                items.pop(key)
