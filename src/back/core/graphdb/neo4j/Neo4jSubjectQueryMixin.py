"""Filtered / counted / inverse subject lookups over the Neo4j property graph.

Mixed into :class:`Neo4jReadOps`; the host supplies ``_conn``, ``_schema``,
``_class_label_for_uri``, ``_known_reltype`` and ``_reltype_for_uri``.
Native-Cypher counterpart of :class:`SubjectQueryMixin` for the SQL stores.
"""

from typing import Any, Dict, List, NamedTuple, Optional

from back.core.errors import ValidationError
from back.core.graphdb.constants import RDFS_LABEL, REVERSE_LOOKUP_MAX_ROWS
from back.core.graphdb.SubjectFilter import SubjectFilter
from back.core.graphdb.SubjectFilterKind import SubjectFilterKind
from back.core.graphdb.neo4j.Neo4jSchemaMap import _NAME_KEY
from back.core.graphdb.neo4j.Neo4jWriteOps import sanitise_label


class _CypherWhere(NamedTuple):
    """AND-combined Cypher predicates on node ``n`` plus their bound params."""

    clauses: List[str]
    params: Dict[str, Any]


class Neo4jSubjectQueryMixin:
    """Cypher builders for filtered / counted / inverse subject lookups."""

    def count_subjects_by_type(
        self,
        table_name: str,
        type_uri: str,
        search: Optional[str] = None,
        filters: Optional[List[SubjectFilter]] = None,
        distinct_predicate: Optional[str] = None,
    ) -> int:
        """Native-Cypher equivalent of the SQL ``count_subjects_by_type``."""
        label = sanitise_label(table_name)
        class_label = self._class_label_for_uri(label, type_uri)
        if class_label is None:
            return 0
        where = self._typed_node_where(label, search, filters)
        if where is None:
            return 0
        params: Dict[str, Any] = dict(where.params)
        match = self._match_where(
            f"MATCH (n:`{label}`:`{class_label}`)", where.clauses
        )
        if not distinct_predicate:
            cypher = match + "RETURN count(DISTINCT n) AS cnt"
        else:
            rt = self._known_reltype(label, distinct_predicate)
            if rt is not None:
                cypher = (
                    match + f"MATCH (n)-[:`{rt}`]->(m:`{label}`) "
                    "RETURN count(DISTINCT m) AS cnt"
                )
            else:
                params["dkey"] = self._prop_key_for_uri(label, distinct_predicate)
                cypher = match + "RETURN count(DISTINCT n[$dkey]) AS cnt"
        rows = self._conn.run(cypher, **params)
        return int((rows or [{}])[0].get("cnt") or 0)

    def find_subjects_by_predicate_objects(
        self,
        table_name: str,
        predicate: str,
        objects: List[str],
        type_uri: Optional[str] = None,
        limit: int = REVERSE_LOOKUP_MAX_ROWS,
    ) -> List[Dict[str, str]]:
        """Reverse-edge lookup: ``(s)-[:predicate]->(o)`` with ``o.uri IN objects``."""
        if not objects:
            return []
        label = sanitise_label(table_name)
        rt = self._reltype_for_uri(label, predicate)
        subject_labels = f"`{label}`"
        if type_uri:
            class_label = self._class_label_for_uri(label, type_uri)
            if class_label is None:
                return []
            subject_labels += f":`{class_label}`"
        rows = self._conn.run(
            f"MATCH (s:{subject_labels})-[:`{rt}`]->(o:`{label}`) "
            f"WHERE o.uri IN $objects "
            f"RETURN DISTINCT s.uri AS subject, o.uri AS object "
            f"ORDER BY subject LIMIT $limit",
            objects=list(objects),
            limit=int(limit),
        )
        return [
            {"subject": r["subject"], "object": r["object"]} for r in (rows or [])
        ]

    def _typed_node_where(
        self,
        label: str,
        search: Optional[str],
        filters: Optional[List[SubjectFilter]],
    ) -> Optional[_CypherWhere]:
        """Cypher predicates on node ``n`` for *search* + *filters*.

        Returns a :class:`_CypherWhere` (empty ``clauses`` when there is
        nothing to filter), or ``None`` when nothing can match: a
        ``relationship`` filter names a predicate this graph has never stored
        as a relationship type. Data-property predicates are not checked for
        existence (an unknown property key simply matches no node).
        """
        clauses: List[str] = []
        params: Dict[str, Any] = {}
        if search:
            params["search"] = search
            clauses.append(
                "any(k IN keys(n) WHERE k <> 'uri' AND "
                "toLower(toString(n[k])) CONTAINS toLower($search))"
            )
        for i, flt in enumerate(filters or []):
            part = self._filter_clause(label, flt, i)
            if part is None:
                return None
            clauses.extend(part.clauses)
            params.update(part.params)
        return _CypherWhere(clauses, params)

    @staticmethod
    def _match_where(match: str, clauses: List[str]) -> str:
        """``match`` plus an optional ``WHERE`` of AND-ed *clauses*.

        Always ends with exactly one space so a ``RETURN`` / ``MATCH`` can be
        appended directly.
        """
        if not clauses:
            return match + " "
        return match + " WHERE " + " AND ".join(clauses) + " "

    def _filter_clause(
        self, label: str, flt: SubjectFilter, index: int
    ) -> Optional[_CypherWhere]:
        """One-clause :class:`_CypherWhere` for filter number *index*.

        Returns ``None`` only for a ``relationship`` filter whose predicate is
        not a known relationship type (nothing can match). Raises
        :class:`ValidationError` for an unknown ``flt.kind``.
        """
        value_param = f"filter_value_{index}"
        v = f"${value_param}"
        params: Dict[str, Any] = {value_param: flt.value}
        if flt.kind == SubjectFilterKind.ID:
            clause = f"({self._local_id_match_cypher('n.uri', v)})"
            return _CypherWhere([clause], params)
        if flt.kind == SubjectFilterKind.RELATIONSHIP:
            rt = self._known_reltype(label, flt.predicate)
            if rt is None:
                return None
            clause = (
                f"any(m IN [(n)-[:`{rt}`]->(x:`{label}`) | x] WHERE "
                f"toLower(coalesce(m.name, '')) CONTAINS toLower({v}) "
                f"OR {self._local_id_match_cypher('m.uri', v)})"
            )
            return _CypherWhere([clause], params)
        if flt.kind == SubjectFilterKind.DATA:
            key_param = f"filter_key_{index}"
            params[key_param] = self._prop_key_for_uri(label, flt.predicate)
            clause = f"toLower(toString(n[${key_param}])) CONTAINS toLower({v})"
            return _CypherWhere([clause], params)
        raise ValidationError(f"Unknown subject filter kind {flt.kind!r}")

    @staticmethod
    def _local_id_match_cypher(
        expr: str, value: str, include_exact: bool = True
    ) -> str:
        """Un-parenthesised OR-chain: *expr* equals *value* or ends with
        ``/`` + *value* / ``#`` + *value* (URI local name).

        *value* is a Cypher expression (normally a ``$param``). With
        ``include_exact=False`` only the two suffix matches are emitted.
        """
        suffix_match = (
            f"{expr} ENDS WITH ('/' + {value}) OR {expr} ENDS WITH ('#' + {value})"
        )
        if not include_exact:
            return suffix_match
        return f"{expr} = {value} OR {suffix_match}"

    def _prop_key_for_uri(self, graph_label: str, predicate_uri: str) -> str:
        """Sanitised node property key for a datatype predicate URI."""
        if predicate_uri == RDFS_LABEL:
            return _NAME_KEY
        prop_map = self._schema.load(graph_label)["prop_map"]
        for key, uri in prop_map.items():
            if uri == predicate_uri:
                return key
        from back.core.graphdb.neo4j.graph_model import reltype_from_predicate

        return reltype_from_predicate(predicate_uri)
