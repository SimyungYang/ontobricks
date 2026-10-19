"""Typed-subject filter, count and reverse-lookup queries for SQL graph stores.

Mixed into :class:`GraphDBBackend`; the host supplies ``_sql_relation``,
``_sql_escape`` and ``execute_query``. Backends that cannot run SQL
(Neo4j) override the public methods.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from back.core.errors import ValidationError
from back.core.graphdb.constants import RDF_TYPE, RDFS_LABEL, REVERSE_LOOKUP_MAX_ROWS
from back.core.graphdb.SubjectFilter import SubjectFilter
from back.core.graphdb.SubjectFilterKind import SubjectFilterKind
from back.core.helpers import LIKE_ESCAPE, sql_like_escape


class SubjectQueryMixin:
    """Stateless SQL builders for filtered / counted / inverse subject lookups."""

    def count_subjects_by_type(
        self,
        table_name: str,
        type_uri: str,
        search: Optional[str] = None,
        filters: Optional[List[SubjectFilter]] = None,
        distinct_predicate: Optional[str] = None,
    ) -> int:
        """Count subjects of *type_uri* matching *search* / *filters*.

        With *distinct_predicate*, count the distinct objects of that
        predicate across the matching subjects instead (e.g. distinct
        customers over the matching sales).
        """
        rel = self._sql_relation(table_name)
        conditions = self._typed_subject_conditions(
            table_name, type_uri, search, filters
        )
        where = " AND ".join(conditions)
        if distinct_predicate:
            esc_pred = self._sql_escape(distinct_predicate)
            sql = (
                f"SELECT COUNT(DISTINCT object) AS cnt FROM {rel} "
                f"WHERE predicate = '{esc_pred}' "
                f"AND subject IN (SELECT subject FROM {rel} WHERE {where})"
            )
        else:
            sql = f"SELECT COUNT(DISTINCT subject) AS cnt FROM {rel} WHERE {where}"
        rows = self.execute_query(sql) or []
        if not rows:
            return 0
        return int(rows[0].get("cnt") or 0)

    def find_subjects_by_predicate_objects(
        self,
        table_name: str,
        predicate: str,
        objects: List[str],
        type_uri: Optional[str] = None,
        limit: int = REVERSE_LOOKUP_MAX_ROWS,
    ) -> List[Dict[str, str]]:
        """Return ``{subject, object}`` rows where ``subject predicate object``.

        Reverse-edge lookup used by GraphQL inverse fields: *objects* is the
        set of target URIs, and the subjects are optionally restricted to
        ``rdf:type`` *type_uri*. Results are capped at *limit* rows.
        """
        if not objects:
            return []
        rel = self._sql_relation(table_name)
        esc_pred = self._sql_escape(predicate)
        in_clause = ", ".join(f"'{self._sql_escape(o)}'" for o in objects)
        conditions = [f"predicate = '{esc_pred}'", f"object IN ({in_clause})"]
        if type_uri:
            esc_type = self._sql_escape(type_uri)
            conditions.append(
                f"subject IN (SELECT subject FROM {rel} "
                f"WHERE predicate = '{RDF_TYPE}' AND object = '{esc_type}')"
            )
        sql = (
            f"SELECT DISTINCT subject, object FROM {rel} "
            f"WHERE {' AND '.join(conditions)} "
            f"ORDER BY subject LIMIT {int(limit)}"
        )
        return self.execute_query(sql) or []

    def _typed_subject_conditions(
        self,
        table_name: str,
        type_uri: str,
        search: Optional[str],
        filters: Optional[List[SubjectFilter]],
    ) -> List[str]:
        """WHERE conditions selecting typed subjects for list / count queries."""
        rel = self._sql_relation(table_name)
        esc_type = self._sql_escape(type_uri)
        conditions = [
            f"predicate = '{RDF_TYPE}'",
            f"object = '{esc_type}'",
        ]
        if search:
            esc = self._sql_escape(sql_like_escape(search)).lower()
            conditions.append(
                f"subject IN ("
                f"SELECT DISTINCT subject FROM {rel} "
                f"WHERE predicate != '{RDF_TYPE}' "
                f"AND LOWER(object) LIKE '%{esc}%' {LIKE_ESCAPE})"
            )
        for flt in filters or []:
            conditions.append(self._subject_filter_condition(rel, flt))
        return conditions

    def _subject_filter_condition(self, rel: str, flt: SubjectFilter) -> str:
        """SQL condition on ``subject`` for one :class:`SubjectFilter`.

        Raises :class:`ValidationError` for an unknown ``flt.kind``.
        """
        esc_val = self._sql_escape(flt.value)
        if flt.kind == SubjectFilterKind.ID:
            return f"({self._local_id_match_sql('subject', esc_val)})"
        if flt.kind == SubjectFilterKind.RELATIONSHIP:
            return self._relationship_filter_sql(rel, flt.predicate, esc_val)
        if flt.kind == SubjectFilterKind.DATA:
            return self._data_filter_sql(rel, flt.predicate, esc_val)
        raise ValidationError(f"Unknown subject filter kind {flt.kind!r}")

    # NOTE: filter values are SQL-quote-escaped but not LIKE-escaped, so a
    # ``%`` or ``_`` in a value acts as a wildcard (known limitation).

    def _relationship_filter_sql(self, rel: str, predicate: str, esc_val: str) -> str:
        """Subjects linked via *predicate* to a target matching by label or id."""
        esc_pred = self._sql_escape(predicate)
        label_match = (
            f"object IN (SELECT subject FROM {rel} "
            f"WHERE predicate = '{RDFS_LABEL}' "
            f"AND LOWER(object) LIKE '%{esc_val.lower()}%')"
        )
        id_match = self._local_id_match_sql("object", esc_val)
        return (
            f"subject IN (SELECT subject FROM {rel} "
            f"WHERE predicate = '{esc_pred}' AND ({label_match} OR {id_match}))"
        )

    def _data_filter_sql(self, rel: str, predicate: str, esc_val: str) -> str:
        """Subjects with a *predicate* literal containing the value (any case)."""
        esc_pred = self._sql_escape(predicate)
        return (
            f"subject IN (SELECT subject FROM {rel} "
            f"WHERE predicate = '{esc_pred}' "
            f"AND LOWER(object) LIKE '%{esc_val.lower()}%')"
        )

    @staticmethod
    def _local_id_match_sql(
        column: str, esc_val: str, include_exact: bool = True
    ) -> str:
        """Un-parenthesised OR-chain: *column* is ``esc_val`` or ends with
        ``/esc_val`` / ``#esc_val`` (URI local name).

        *esc_val* must already be SQL-escaped. With ``include_exact=False``
        only the two suffix matches are emitted.
        """
        suffix_match = f"{column} LIKE '%/{esc_val}' OR {column} LIKE '%#{esc_val}'"
        if not include_exact:
            return suffix_match
        return f"{column} = '{esc_val}' OR {suffix_match}"
