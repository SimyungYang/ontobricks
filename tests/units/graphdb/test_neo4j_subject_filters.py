"""Neo4j backend: filtered lookups, counts and inverse (reverse-edge) lookups.

The driver is mocked; we assert the emitted Cypher and parameters.
"""

import json
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from back.core.graphdb.SubjectFilter import SubjectFilter

try:
    import neo4j  # noqa: F401

    NEO4J_AVAILABLE = True
except ImportError:
    NEO4J_AVAILABLE = False

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not NEO4J_AVAILABLE, reason="neo4j driver not installed"),
]

BASE = "https://ex.org/dom/"
SCHEMA_ROW = [
    {
        "label_map": json.dumps({"Sale": f"{BASE}Sale", "Customer": f"{BASE}Customer"}),
        "reltype_map": json.dumps({"soldTo": f"{BASE}soldTo"}),
        "prop_map": json.dumps({"tier": f"{BASE}tier"}),
    }
]


def _store(result_rows):
    from back.core.graphdb.neo4j.Neo4jStore import Neo4jStore

    cfg: Dict[str, Any] = {
        "uri": "neo4j+s://x.databases.neo4j.io",
        "database": "neo4j",
        "auth_method": "basic",
        "username": "neo4j",
        "password": "pw",
    }
    s = Neo4jStore(db_name="testset", engine_config=cfg)

    def run(cypher, **params):
        if "__GraphSchema" in cypher:
            return SCHEMA_ROW
        return result_rows

    s._connection.run = MagicMock(side_effect=run)  # type: ignore[assignment]
    return s


def _last_call(store):
    return store._connection.run.call_args


class TestNeo4jFilters:
    def test_relationship_filter_uses_reltype_pattern(self):
        s = _store([{"subject": f"{BASE}Sale/1"}])
        s.find_subjects_by_type(
            "dom_V1",
            f"{BASE}Sale",
            filters=[SubjectFilter(SubjectFilter.RELATIONSHIP, "Kim", f"{BASE}soldTo")],
        )
        assert "(n)-[:`soldTo`]->" in _last_call(s).args[0]

    def test_unknown_relationship_predicate_matches_nothing(self):
        s = _store([{"subject": f"{BASE}Sale/1"}])
        out = s.find_subjects_by_type(
            "dom_V1",
            f"{BASE}Sale",
            filters=[SubjectFilter(SubjectFilter.RELATIONSHIP, "x", f"{BASE}nope")],
        )
        assert out == []

    def test_data_filter_binds_property_key(self):
        s = _store([])
        s.find_subjects_by_type(
            "dom_V1",
            f"{BASE}Customer",
            filters=[SubjectFilter(SubjectFilter.DATA, "VIP", f"{BASE}tier")],
        )
        assert _last_call(s).kwargs["filter_key_0"] == "tier"

    def test_unknown_kind_raises_validation_error(self):
        from back.core.errors import ValidationError

        s = _store([])
        with pytest.raises(ValidationError):
            s.find_subjects_by_type(
                "dom_V1", f"{BASE}Sale", filters=[SubjectFilter("bogus", "x")]
            )


class TestNeo4jCountAndInverse:
    def test_count_returns_cnt(self):
        s = _store([{"cnt": 5}])
        assert s.count_subjects_by_type("dom_V1", f"{BASE}Sale") == 5

    def test_count_distinct_relationship_targets(self):
        s = _store([{"cnt": 2}])
        s.count_subjects_by_type(
            "dom_V1", f"{BASE}Sale", distinct_predicate=f"{BASE}soldTo"
        )
        assert "count(DISTINCT m)" in _last_call(s).args[0]

    def test_inverse_lookup_matches_objects(self):
        s = _store([{"subject": f"{BASE}Sale/1", "object": f"{BASE}Customer/1"}])
        rows = s.find_subjects_by_predicate_objects(
            "dom_V1", f"{BASE}soldTo", [f"{BASE}Customer/1"], type_uri=f"{BASE}Sale"
        )
        assert rows == [{"subject": f"{BASE}Sale/1", "object": f"{BASE}Customer/1"}]

    def test_inverse_lookup_passes_object_list(self):
        s = _store([])
        s.find_subjects_by_predicate_objects(
            "dom_V1", f"{BASE}soldTo", [f"{BASE}Customer/1"]
        )
        assert _last_call(s).kwargs["objects"] == [f"{BASE}Customer/1"]
