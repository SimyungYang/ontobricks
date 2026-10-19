"""SQL emitted by GraphDBBackend for filtered / counted / inverse lookups."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from back.core.errors import ValidationError
from back.core.graphdb import SubjectFilter, SubjectFilterKind
from back.core.graphdb.GraphDBBackend import GraphDBBackend
from back.core.graphdb.constants import RDF_TYPE, RDFS_LABEL

pytestmark = pytest.mark.unit

T = "cat.sch.triples"
P_SOLD_TO = "http://x.org/soldTo"
C_SALE = "http://x.org/SalesTransaction"


class CaptureStore(GraphDBBackend):
    """Records the SQL instead of running it."""

    def __init__(self, rows: List[Dict[str, Any]] | None = None) -> None:
        self.sql: List[str] = []
        self._rows = rows or []

    def execute_query(self, query: str) -> List[Dict[str, Any]]:
        self.sql.append(query)
        return list(self._rows)

    def create_table(self, table_name):  # pragma: no cover
        pass

    def drop_table(self, table_name):  # pragma: no cover
        pass

    def insert_triples(self, table_name, triples, **_kw):  # pragma: no cover
        pass

    def query_triples(self, table_name):  # pragma: no cover
        return []

    def count_triples(self, table_name):  # pragma: no cover
        return 0

    def table_exists(self, table_name):  # pragma: no cover
        return True

    def get_status(self, table_name):  # pragma: no cover
        return {}

    def get_connection(self):  # pragma: no cover
        return None

    def close(self):  # pragma: no cover
        pass


class TestInverseLookupSQL:
    def _sql(self, **kw) -> str:
        store = CaptureStore()
        store.find_subjects_by_predicate_objects(
            T, P_SOLD_TO, ["http://x.org/C/1", "http://x.org/C/2"], **kw
        )
        return store.sql[0]

    def test_filters_on_predicate(self):
        assert f"predicate = '{P_SOLD_TO}'" in self._sql()

    def test_filters_objects_with_in_list(self):
        assert "object IN ('http://x.org/C/1', 'http://x.org/C/2')" in self._sql()

    def test_restricts_subject_type_when_given(self):
        sql = self._sql(type_uri=C_SALE)
        assert (
            f"subject IN (SELECT subject FROM {T} WHERE predicate = '{RDF_TYPE}' "
            f"AND object = '{C_SALE}')"
        ) in sql

    def test_applies_row_limit(self):
        assert self._sql(limit=7).endswith("ORDER BY subject LIMIT 7")

    def test_escapes_quotes_in_objects(self):
        store = CaptureStore()
        store.find_subjects_by_predicate_objects(T, P_SOLD_TO, ["http://x/O'Brien"])
        assert "'http://x/O''Brien'" in store.sql[0]

    def test_empty_objects_issue_no_query(self):
        store = CaptureStore()
        assert store.find_subjects_by_predicate_objects(T, P_SOLD_TO, []) == []
        assert store.sql == []


class TestSubjectFilterSQL:
    def _sql(self, flt: SubjectFilter) -> str:
        store = CaptureStore()
        store.find_subjects_by_type(T, C_SALE, filters=[flt])
        return store.sql[0]

    def test_relationship_filter_matches_target_label(self):
        sql = self._sql(SubjectFilter(SubjectFilter.RELATIONSHIP, "Gangnam", P_SOLD_TO))
        assert (
            f"WHERE predicate = '{RDFS_LABEL}' AND LOWER(object) LIKE '%gangnam%'"
        ) in sql

    def test_relationship_filter_matches_target_local_name(self):
        sql = self._sql(SubjectFilter(SubjectFilter.RELATIONSHIP, "CUST1", P_SOLD_TO))
        assert "OR object LIKE '%/CUST1' OR object LIKE '%#CUST1'" in sql

    def test_data_filter_is_lowercase_contains(self):
        sql = self._sql(SubjectFilter(SubjectFilter.DATA, "VIP", "http://x.org/tier"))
        assert (
            "WHERE predicate = 'http://x.org/tier' AND LOWER(object) LIKE '%vip%'"
        ) in sql

    def test_id_filter_matches_local_name(self):
        sql = self._sql(SubjectFilter(SubjectFilter.ID, "SAL1"))
        assert "subject LIKE '%/SAL1'" in sql

    def test_filter_value_is_escaped(self):
        sql = self._sql(SubjectFilter(SubjectFilter.DATA, "O'Brien", "http://x.org/n"))
        assert "'%o''brien%'" in sql

    def test_plain_string_kind_matches_enum(self):
        sql = self._sql(SubjectFilter("id", "SAL1"))
        assert "(subject = 'SAL1' OR subject LIKE '%/SAL1' OR subject LIKE '%#SAL1')" in sql

    def test_enum_kind_equals_string_value(self):
        assert SubjectFilterKind.RELATIONSHIP == "relationship"

    def test_unknown_kind_raises_validation_error(self):
        with pytest.raises(ValidationError):
            self._sql(SubjectFilter("bogus", "x", "http://x.org/p"))

    def test_unfiltered_query_is_unchanged(self):
        store = CaptureStore()
        store.find_subjects_by_type(T, C_SALE, limit=5, offset=10)
        assert store.sql[0] == (
            f"SELECT DISTINCT subject FROM {T} WHERE predicate = '{RDF_TYPE}' "
            f"AND object = '{C_SALE}' ORDER BY subject LIMIT 5 OFFSET 10"
        )


class TestCountSQL:
    def test_counts_distinct_subjects(self):
        store = CaptureStore(rows=[{"cnt": 42}])
        assert store.count_subjects_by_type(T, C_SALE) == 42
        assert store.sql[0].startswith(f"SELECT COUNT(DISTINCT subject) AS cnt FROM {T}")

    def test_distinct_predicate_counts_objects(self):
        store = CaptureStore(rows=[{"cnt": 3}])
        store.count_subjects_by_type(T, C_SALE, distinct_predicate=P_SOLD_TO)
        assert store.sql[0].startswith(
            f"SELECT COUNT(DISTINCT object) AS cnt FROM {T} "
            f"WHERE predicate = '{P_SOLD_TO}' AND subject IN (SELECT subject FROM {T}"
        )

    def test_empty_result_counts_zero(self):
        assert CaptureStore(rows=[]).count_subjects_by_type(T, C_SALE) == 0
