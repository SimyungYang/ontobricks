"""GraphQL schema metadata registry and entity resolution."""

import dataclasses
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Set, Tuple

from back.core.errors import InfrastructureError, ValidationError
from back.core.graphdb.SubjectFilter import SubjectFilter
from back.core.helpers import extract_local_name as _extract_local
from back.core.logging import get_logger

from back.core.graphql.constants import (
    DEFAULT_DEPTH,
    DEFAULT_LIST_LIMIT,
    INVERSE_MAX_ROWS,
    MAX_DEPTH,
    MAX_LIST_LIMIT,
    RDF_TYPE,
    RDFS_LABEL,
)
from back.core.graphql.models import TypeInfo

if TYPE_CHECKING:
    from back.core.graphql.FieldFilter import FieldFilter

logger = get_logger(__name__)

# A selection tree maps GraphQL field names to their sub-selection tree
# (an empty dict for scalar leaves). ``None`` means "no selection info"; see
# ``SchemaMetadata._build_entities`` for how each shape is resolved.
Selection = Optional[Dict[str, dict]]

# Targets of one forward relationship field: ``(field name, target type)``.
_RelKey = Tuple[str, str]

_STUB_FIELDS = frozenset({"id", "uri", "__typename"})


class SchemaMetadata:
    """Registry of all generated GraphQL types and their resolution logic.

    Created once during schema generation, then shared with every resolver
    via closure so they can build typed entity instances from triple-store
    rows.

    Resolution is **selection-aware** when the resolver passes the query's
    selection tree: only selected relationship / inverse fields are loaded.
    Without selection info the legacy behaviour applies (every forward
    relationship eagerly resolved to ``depth``; inverse fields untouched).
    """

    def __init__(self, base_uri: str = ""):
        self.types: Dict[str, TypeInfo] = {}
        self.base_uri = base_uri
        self._digest: Optional[str] = None

    def register(self, info: TypeInfo) -> None:
        self.types[info.name] = info
        self.reset_digest()

    def reset_digest(self) -> None:
        """Drop the memoised digest (call after mutating a registered TypeInfo)."""
        self._digest = None

    def digest(self) -> str:
        """Compact schema digest for agent prompts (memoised per schema)."""
        if self._digest is None:
            from back.core.graphql.SchemaDigest import SchemaDigest

            self._digest = SchemaDigest.render(self)
        return self._digest

    # ------------------------------------------------------------------
    # Public resolver entry points
    # ------------------------------------------------------------------

    def resolve_list(
        self,
        store: Any,
        table: str,
        type_name: str,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
        search: Optional[str] = None,
        depth: Optional[int] = None,
        filters: Optional[Sequence["FieldFilter"]] = None,
        selection: Selection = None,
    ) -> list:
        info = self.types.get(type_name)
        if not info:
            logger.warning(
                "GraphQL resolve_list: unknown type '%s'. Registered types: %s",
                type_name,
                list(self.types.keys()),
            )
            return []

        limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        offset = max(0, int(offset))
        subject_filters = self._subject_filters(info, filters)

        logger.info(
            "GraphQL resolve_list: type=%s, cls_uri=%s, table=%s, search=%s, "
            "filters=%d, limit=%d",
            type_name,
            info.cls_uri,
            table,
            search,
            len(subject_filters),
            limit,
        )

        uris = self._query_subjects(
            store, table, info.cls_uri, limit, offset, search, subject_filters
        )
        if not uris:
            logger.warning(
                "GraphQL resolve_list: no subjects found for type=%s (cls_uri=%s, search=%s). "
                "Try the /graphql/{domain}/debug?type_name=%s endpoint to diagnose.",
                type_name,
                info.cls_uri,
                search,
                type_name,
            )
            return []

        effective_depth = self._effective_depth(depth, selection)
        logger.info(
            "GraphQL resolve_list %s: %d URIs found, depth=%d, selection_aware=%s",
            type_name,
            len(uris),
            effective_depth,
            selection is not None,
        )
        return self._build_entities(
            store, table, info, uris, depth=effective_depth, selection=selection
        )

    def resolve_single(
        self,
        store: Any,
        table: str,
        type_name: str,
        entity_id: str,
        depth: Optional[int] = None,
        selection: Selection = None,
    ) -> Any:
        info = self.types.get(type_name)
        if not info:
            return None

        uri = self._resolve_id_to_uri(store, table, info.cls_uri, entity_id)
        if not uri:
            return None

        entities = self._build_entities(
            store,
            table,
            info,
            [uri],
            depth=self._effective_depth(depth, selection),
            selection=selection,
        )
        return entities[0] if entities else None

    def resolve_count(
        self,
        store: Any,
        table: str,
        type_name: str,
        search: Optional[str] = None,
        filters: Optional[Sequence["FieldFilter"]] = None,
        distinct_by: Optional[str] = None,
    ) -> int:
        """Exact count of matching entities (or distinct ``distinct_by`` values)."""
        info = self.types.get(type_name)
        if not info:
            return 0
        subject_filters = self._subject_filters(info, filters)
        distinct_predicate = None
        if distinct_by:
            distinct_predicate = self._predicate_for_field(info, distinct_by)
        try:
            return int(
                store.count_subjects_by_type(
                    table,
                    info.cls_uri,
                    search=search,
                    filters=subject_filters,
                    distinct_predicate=distinct_predicate,
                )
            )
        except Exception as e:
            logger.error("GraphQL resolve_count failed for %s: %s", type_name, e)
            raise InfrastructureError(
                f"Count query failed for {type_name}", detail=str(e)
            ) from e

    # ------------------------------------------------------------------
    # Filter translation
    # ------------------------------------------------------------------

    def _subject_filters(
        self, info: TypeInfo, filters: Optional[Sequence["FieldFilter"]]
    ) -> List[SubjectFilter]:
        """Translate GraphQL ``FieldFilter`` inputs into store-level filters.

        Raises :class:`ValidationError` naming the valid fields when a field
        is unknown, so the caller (often an LLM) can self-correct.
        """
        out: List[SubjectFilter] = []
        for flt in filters or []:
            field_name = flt.field
            value = (flt.value or "").strip()
            if not value:
                raise ValidationError(f"Filter on {field_name!r} needs a non-empty value")
            if field_name == "id":
                out.append(SubjectFilter(SubjectFilter.ID, value))
                continue
            if field_name == "label":
                out.append(SubjectFilter(SubjectFilter.DATA, value, RDFS_LABEL))
                continue
            py_name = info.python_field(field_name)
            if py_name in info.relationships:
                pred = info.relationships[py_name].predicate
                out.append(SubjectFilter(SubjectFilter.RELATIONSHIP, value, pred))
            elif py_name in info.field_to_predicate:
                pred = info.field_to_predicate[py_name]
                out.append(SubjectFilter(SubjectFilter.DATA, value, pred))
            else:
                raise ValidationError(self._unknown_field_message(info, field_name))
        return out

    def _predicate_for_field(self, info: TypeInfo, field_name: str) -> str:
        if field_name == "label":
            return RDFS_LABEL
        py_name = info.python_field(field_name)
        if py_name in info.field_to_predicate:
            return info.field_to_predicate[py_name]
        raise ValidationError(self._unknown_field_message(info, field_name))

    @staticmethod
    def _unknown_field_message(info: TypeInfo, field_name: str) -> str:
        valid = ["id", "label"] + [
            info.gql_name(f) for f in info.field_to_predicate
        ]
        return (
            f"Unknown field {field_name!r} for {info.name}. "
            f"Valid fields: {', '.join(valid)}"
        )

    # ------------------------------------------------------------------
    # Triple-store queries
    # ------------------------------------------------------------------

    def _query_subjects(
        self,
        store: Any,
        table: str,
        cls_uri: str,
        limit: int,
        offset: int,
        search: Optional[str],
        filters: Optional[List[SubjectFilter]] = None,
    ) -> List[str]:
        try:
            return store.find_subjects_by_type(
                table, cls_uri, limit, offset, search, filters=filters
            )
        except Exception as e:
            logger.error("GraphQL _query_subjects failed: %s", e)
            # Unfiltered failures keep the legacy "empty list" result; a filtered
            # [] would read as "no match", so filtered failures raise instead.
            if filters:
                raise InfrastructureError(
                    "Filtered GraphQL lookup failed", detail=str(e)
                ) from e
            return []

    def _resolve_id_to_uri(
        self, store: Any, table: str, cls_uri: str, entity_id: str
    ) -> Optional[str]:
        try:
            return store.resolve_subject_by_id(table, cls_uri, entity_id)
        except Exception as e:
            logger.error("GraphQL _resolve_id_to_uri failed: %s", e)
            return None

    def _load_triples(
        self, store: Any, table: str, uris: List[str]
    ) -> Dict[str, List[Dict[str, str]]]:
        if not uris:
            return {}

        result: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        try:
            for r in store.get_triples_for_subjects(table, uris):
                result[r["subject"]].append(r)
        except Exception as e:
            logger.error("GraphQL _load_triples failed: %s", e)
        return result

    def _query_inverse(
        self,
        store: Any,
        table: str,
        predicate: str,
        object_uris: List[str],
        source_cls_uri: str,
    ) -> List[Dict[str, str]]:
        try:
            rows = store.find_subjects_by_predicate_objects(
                table,
                predicate,
                object_uris,
                type_uri=source_cls_uri,
                limit=INVERSE_MAX_ROWS,
            )
        except Exception as e:
            logger.error("GraphQL inverse lookup failed for %s: %s", predicate, e)
            raise InfrastructureError(
                "Inverse relationship lookup failed", detail=str(e)
            ) from e
        if len(rows) >= INVERSE_MAX_ROWS:
            logger.warning(
                "GraphQL inverse lookup for %s hit the %d-row cap; lists are partial",
                predicate,
                INVERSE_MAX_ROWS,
            )
        return rows

    # ------------------------------------------------------------------
    # Entity construction
    # ------------------------------------------------------------------

    @staticmethod
    def _effective_depth(depth: Optional[int], selection: Selection) -> int:
        if depth is not None:
            return min(depth, MAX_DEPTH)
        # Selection-aware queries are bounded by the query's own nesting.
        return MAX_DEPTH if selection is not None else DEFAULT_DEPTH

    @staticmethod
    def _child_selection(
        info: TypeInfo, field_name: str, selection: Selection
    ) -> Tuple[bool, Selection]:
        """``(is_selected, sub_selection)`` for one relationship field."""
        if selection is None:
            return True, None
        gql = info.gql_name(field_name)
        if gql not in selection:
            return False, None
        return True, selection.get(gql) or {}

    def _build_entities(
        self,
        store: Any,
        table: str,
        type_info: TypeInfo,
        uris: List[str],
        depth: int = 1,
        selection: Selection = None,
    ) -> list:
        """Build *type_info* entities for *uris*, resolving relationships.

        *selection* is the GraphQL sub-selection of these entities:

        * ``None`` -- no selection info: legacy eager load. Every forward
          relationship is resolved down to *depth*; inverse fields stay unset.
        * ``{}`` (or only ``id`` / ``uri`` / ``__typename``) -- nothing below
          identity selected: stub entities, no triple load at all.
        * ``{field: sub_selection, ...}`` -- only the selected forward and
          inverse relationship fields are loaded, recursively with their own
          sub-selection.
        """
        if selection is not None and set(selection) <= _STUB_FIELDS:
            return [self._stub_entity(type_info, uri) for uri in uris]

        triples_by_subject = self._load_triples(store, table, uris)
        entities = [
            self._assemble_entity(type_info, uri, triples_by_subject.get(uri, []))
            for uri in uris
        ]
        resolved: Dict[_RelKey, Dict[str, Any]] = {}
        if depth > 0:
            targets, rel_selection = self._collect_rel_targets(
                type_info, entities, selection
            )
            resolved = self._resolve_rel_targets(
                store, table, targets, rel_selection, depth
            )
        result = self._apply_rel_targets(type_info, entities, resolved)

        if selection is not None and depth > 0:
            self._attach_inverse(store, table, type_info, result, depth, selection)
        return result

    def _collect_rel_targets(
        self,
        type_info: TypeInfo,
        entities: List[Tuple[Any, Dict[str, List[str]]]],
        selection: Selection,
    ) -> Tuple[Dict[_RelKey, Set[str]], Dict[str, Selection]]:
        """Target URIs per selected relationship field, plus each field's
        sub-selection, gathered across all *entities* (one batch per field)."""
        targets: Dict[_RelKey, Set[str]] = defaultdict(set)
        rel_selection: Dict[str, Selection] = {}
        for _entity, pending in entities:
            for field_name, target_uris in pending.items():
                rel = type_info.relationships.get(field_name)
                if not rel:
                    continue
                selected, sub = self._child_selection(type_info, field_name, selection)
                if not selected:
                    continue
                rel_selection[field_name] = sub
                targets[(field_name, rel.target_type)].update(target_uris)
        return targets, rel_selection

    def _resolve_rel_targets(
        self,
        store: Any,
        table: str,
        targets: Dict[_RelKey, Set[str]],
        rel_selection: Dict[str, Selection],
        depth: int,
    ) -> Dict[_RelKey, Dict[str, Any]]:
        """Build the target entities of each relationship field one level down."""
        resolved: Dict[_RelKey, Dict[str, Any]] = {}
        for (field_name, target_type), t_uris in targets.items():
            target_info = self.types.get(target_type)
            if not target_info:
                continue
            target_entities = self._build_entities(
                store,
                table,
                target_info,
                sorted(t_uris),
                depth=depth - 1,
                selection=rel_selection.get(field_name),
            )
            resolved[(field_name, target_type)] = {
                getattr(e, "uri", ""): e for e in target_entities
            }
        return resolved

    def _apply_rel_targets(
        self,
        type_info: TypeInfo,
        entities: List[Tuple[Any, Dict[str, List[str]]]],
        resolved: Dict[_RelKey, Dict[str, Any]],
    ) -> list:
        """Set each resolved relationship field on its entity; return entities."""
        result = []
        for entity, pending in entities:
            for field_name, target_uris in pending.items():
                rel = type_info.relationships.get(field_name)
                if not rel:
                    continue
                uri_map = resolved.get((field_name, rel.target_type))
                if uri_map is None:
                    continue
                related = [uri_map[u] for u in target_uris if u in uri_map]
                self._set_field(entity, field_name, related)
            result.append(entity)
        return result

    def _attach_inverse(
        self,
        store: Any,
        table: str,
        type_info: TypeInfo,
        entities: list,
        depth: int,
        selection: Dict[str, dict],
    ) -> None:
        """Resolve the selected inverse fields of *entities* in one batch each."""
        uris = [getattr(e, "uri", "") for e in entities]
        for fname, inverse in type_info.inverse_relationships.items():
            selected, sub = self._child_selection(type_info, fname, selection)
            source_info = self.types.get(inverse.source_type)
            if not selected or not source_info:
                continue
            rows = self._query_inverse(
                store, table, inverse.predicate, uris, source_info.cls_uri
            )
            by_object: Dict[str, List[str]] = defaultdict(list)
            ordered_sources: List[str] = []
            seen: Set[str] = set()
            for r in rows:
                subj, obj = r.get("subject", ""), r.get("object", "")
                if not subj:
                    continue
                by_object[obj].append(subj)
                if subj not in seen:
                    seen.add(subj)
                    ordered_sources.append(subj)
            sources = self._build_entities(
                store, table, source_info, ordered_sources, depth - 1, sub
            )
            uri_to_source = {getattr(s, "uri", ""): s for s in sources}
            for entity in entities:
                linked = [
                    uri_to_source[u]
                    for u in by_object.get(getattr(entity, "uri", ""), [])
                    if u in uri_to_source
                ]
                self._set_field(entity, fname, linked)

    @staticmethod
    def _set_field(entity: Any, field_name: str, values: list) -> None:
        try:
            object.__setattr__(entity, field_name, values or None)
        except (AttributeError, TypeError):
            pass

    @staticmethod
    def _stub_entity(type_info: TypeInfo, uri: str) -> Any:
        return type_info.gql_type(id=_extract_local(uri), uri=uri)

    def _assemble_entity(
        self,
        type_info: TypeInfo,
        uri: str,
        triples: List[Dict[str, str]],
    ) -> Tuple[Any, Dict[str, List[str]]]:
        """Build a single entity instance and return pending relationship URIs."""
        kwargs: Dict[str, Any] = {
            "id": _extract_local(uri),
            "uri": uri,
            "label": None,
        }
        pending_rels: Dict[str, List[str]] = defaultdict(list)

        _logged_diag = False
        for t in triples:
            pred = t["predicate"]
            obj = t["object"]

            if pred == RDFS_LABEL:
                kwargs["label"] = obj
                continue

            field_name = type_info.predicate_to_field.get(pred)
            if not field_name:
                if not _logged_diag and pred != RDF_TYPE:
                    store_preds = [
                        tr["predicate"]
                        for tr in triples
                        if tr["predicate"] != RDF_TYPE and tr["predicate"] != RDFS_LABEL
                    ]
                    logger.warning(
                        "GraphQL predicate mismatch for %s (%s). "
                        "Triple-store predicates: %s  |  "
                        "Schema predicate_to_field keys: %s",
                        type_info.name,
                        uri,
                        store_preds[:8],
                        list(type_info.predicate_to_field.keys())[:8],
                    )
                    _logged_diag = True
                continue

            if field_name in type_info.relationships:
                pending_rels[field_name].append(obj)
            else:
                kwargs[field_name] = obj

        known = {f.name for f in dataclasses.fields(type_info.gql_type)}
        filtered = {k: v for k, v in kwargs.items() if k in known}

        try:
            entity = type_info.gql_type(**filtered)
        except Exception as e:
            logger.warning(
                "GraphQL entity build failed for %s (%s): %s",
                type_info.name,
                uri,
                e,
            )
            entity = type_info.gql_type(id=_extract_local(uri), uri=uri)

        return entity, dict(pending_rels)
