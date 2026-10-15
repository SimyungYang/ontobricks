"""End-to-end-ish tests for GraphQL inverse fields, filters, counts and
selection-aware resolution.

The schema is built from a small Customer / Product / SalesTransaction
ontology and executed against a :class:`GraphDBBackend` whose
``execute_query`` runs the *real* generated SQL on in-memory SQLite, so the
store SQL (filters, counts, inverse lookup) is exercised end to end.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

import pytest

from back.core.graphdb.GraphDBBackend import GraphDBBackend
from back.core.graphdb.constants import RDF_TYPE, RDFS_LABEL
from back.core.graphql import GraphQLSchemaBuilder
from back.core.graphql.constants import MAX_LIST_LIMIT
from agents.tools.graph_formatting import format_graphql_response

pytestmark = pytest.mark.unit

B = "http://lge.test/onto/"
D = "http://lge.test/data/"
TABLE = "triples"


class SQLiteTripleStore(GraphDBBackend):
    """Triple store whose SQL runs on in-memory SQLite (test double)."""

    def __init__(self) -> None:
        self._db = sqlite3.connect(":memory:")
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            f"CREATE TABLE {TABLE} (subject TEXT, predicate TEXT, object TEXT)"
        )
        self.queries: List[str] = []
        self.loaded_subjects: List[List[str]] = []

    def add(self, s: str, p: str, o: str) -> None:
        self._db.execute(f"INSERT INTO {TABLE} VALUES (?, ?, ?)", (s, p, o))

    def execute_query(self, query: str) -> List[Dict[str, Any]]:
        self.queries.append(query)
        return [dict(r) for r in self._db.execute(query).fetchall()]

    def get_triples_for_subjects(self, table_name, subjects):
        self.loaded_subjects.append(list(subjects))
        return super().get_triples_for_subjects(table_name, subjects)

    # Abstract CRUD surface (unused here).
    def create_table(self, table_name):  # pragma: no cover
        pass

    def drop_table(self, table_name):  # pragma: no cover
        pass

    def insert_triples(self, table_name, triples, **_kw):  # pragma: no cover
        pass

    def query_triples(self, table_name):  # pragma: no cover
        return self.execute_query(f"SELECT * FROM {TABLE}")

    def count_triples(self, table_name):  # pragma: no cover
        return 0

    def table_exists(self, table_name):  # pragma: no cover
        return True

    def get_status(self, table_name):  # pragma: no cover
        return {}

    def get_connection(self):  # pragma: no cover
        return self._db

    def close(self):  # pragma: no cover
        self._db.close()


CLASSES = [
    {
        "uri": f"{B}Customer",
        "name": "Customer",
        "label": "Customer",
        "dataProperties": [{"name": "customerId", "uri": f"{B}customerId"}],
    },
    {
        "uri": f"{B}Product",
        "name": "Product",
        "dataProperties": [{"name": "productId", "uri": f"{B}productId"}],
    },
    {"uri": f"{B}SalesOrganization", "name": "SalesOrganization"},
    {"uri": f"{B}SalesChannel", "name": "SalesChannel"},
    {
        "uri": f"{B}SalesTransaction",
        "name": "SalesTransaction",
        "label": "Sales transaction",
        "dataProperties": [
            {"name": "saleDate", "uri": f"{B}saleDate"},
            {"name": "grossKrw", "uri": f"{B}grossKrw"},
        ],
    },
]


def _rel(name: str, domain: str, range_: str, label: str = "") -> Dict[str, str]:
    return {
        "uri": f"{B}{name}",
        "name": name,
        "label": label,
        "domain": f"{B}{domain}",
        "range": f"{B}{range_}",
    }


PROPERTIES = [
    _rel("soldTo", "SalesTransaction", "Customer", "Buyer"),
    _rel("ofProduct", "SalesTransaction", "Product"),
    _rel("bySalesOrg", "SalesTransaction", "SalesOrganization"),
    _rel("viaChannel", "SalesTransaction", "SalesChannel"),
]

# (id, customer, product, org, channel)
SALES = [
    ("SAL001", "CUST00174", "PRD01", "ORG_GN", "CH_SHOP"),
    ("SAL002", "CUST00174", "PRD02", "ORG_BS", "CH_MALL"),
    ("SAL003", "CUST00370", "PRD03", "ORG_GN", "CH_SHOP"),
    ("SAL004", "CUST00500", "PRD01", "ORG_GN", "CH_MALL"),
    ("SAL005", "CUST00500", "PRD03", "ORG_GN", "CH_SHOP"),
    ("SAL006", "CUST00500", "PRD02", "ORG_GN", "CH_SHOP"),
]


def _populate(store: SQLiteTripleStore) -> None:
    entities = {
        ("Customer", "CUST00174"): "홍길동",
        ("Customer", "CUST00370"): "홍길동",
        ("Customer", "CUST00500"): "김철수",
        ("Product", "PRD01"): "VisionTV C4 55",
        ("Product", "PRD02"): "AirPro 에어컨",
        ("Product", "PRD03"): "VisionTV G4 65",
        ("SalesOrganization", "ORG_GN"): "중앙지점",
        ("SalesOrganization", "ORG_BS"): "부산지점",
        ("SalesChannel", "CH_SHOP"): "데모매장",
        ("SalesChannel", "CH_MALL"): "Demo 공식몰",
    }
    for (cls, ident), label in entities.items():
        uri = f"{D}{cls}/{ident}"
        store.add(uri, RDF_TYPE, f"{B}{cls}")
        store.add(uri, RDFS_LABEL, label)
        if cls == "Customer":
            store.add(uri, f"{B}customerId", ident)
        if cls == "Product":
            store.add(uri, f"{B}productId", ident)
    for sid, cust, prod, org, chan in SALES:
        uri = f"{D}SalesTransaction/{sid}"
        store.add(uri, RDF_TYPE, f"{B}SalesTransaction")
        store.add(uri, f"{B}saleDate", "2025-01-01")
        store.add(uri, f"{B}soldTo", f"{D}Customer/{cust}")
        store.add(uri, f"{B}ofProduct", f"{D}Product/{prod}")
        store.add(uri, f"{B}bySalesOrg", f"{D}SalesOrganization/{org}")
        store.add(uri, f"{B}viaChannel", f"{D}SalesChannel/{chan}")


@pytest.fixture
def built():
    schema, metadata = GraphQLSchemaBuilder().build_for_domain(
        CLASSES, PROPERTIES, B, "lge_test"
    )
    return schema, metadata


@pytest.fixture
def store():
    s = SQLiteTripleStore()
    _populate(s)
    return s


def _run(schema, store, query: str) -> Dict[str, Any]:
    result = schema.execute_sync(
        query, context_value={"triplestore": store, "table_name": TABLE}
    )
    assert result.errors is None, result.errors
    return result.data


def _ids(rows: List[Dict[str, Any]]) -> List[str]:
    return sorted(r["id"] for r in rows)


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


class TestInverseFieldSchema:
    def test_inverse_field_in_sdl(self, built):
        from strawberry.printer import print_schema

        assert "salesTransactionsViaSoldTo: [SalesTransaction!]" in print_schema(
            built[0]
        )

    def test_count_root_field_in_sdl(self, built):
        from strawberry.printer import print_schema

        assert "salesTransactionsCount(" in print_schema(built[0])

    def test_filters_argument_in_sdl(self, built):
        from strawberry.printer import print_schema

        assert "filters: [FieldFilter!]" in print_schema(built[0])

    def test_inverse_registered_in_metadata(self, built):
        info = built[1].types["Customer"]
        assert info.inverse_relationships["salesTransactionsViaSoldTo"] == (
            f"{B}soldTo",
            "SalesTransaction",
        )

    def test_inverse_field_name_is_deterministic(self):
        assert (
            GraphQLSchemaBuilder.inverse_field_name("SalesTransaction", "soldTo")
            == "salesTransactionsViaSoldTo"
        )

    def test_self_reference_and_cycle_build(self):
        classes = [
            {"uri": f"{B}Employee", "name": "Employee"},
            {"uri": f"{B}Team", "name": "Team"},
        ]
        props = [
            _rel("managedBy", "Employee", "Employee"),
            _rel("memberOf", "Employee", "Team"),
            _rel("ledBy", "Team", "Employee"),
        ]
        result = GraphQLSchemaBuilder().build_for_domain(classes, props, B, "cyc")
        assert result is not None
        assert "employeesViaManagedBy" in result[1].types["Employee"].inverse_relationships

    def test_inverse_name_collision_is_skipped(self):
        classes = [dict(c) for c in CLASSES]
        classes[0] = {
            **classes[0],
            "dataProperties": [
                {"name": "salesTransactionsViaSoldTo", "uri": f"{B}clash"}
            ],
        }
        result = GraphQLSchemaBuilder().build_for_domain(
            classes, PROPERTIES, B, "clash"
        )
        assert result is not None
        assert "salesTransactionsViaSoldTo" not in (
            result[1].types["Customer"].inverse_relationships
        )


# ---------------------------------------------------------------------------
# Inverse resolution
# ---------------------------------------------------------------------------


class TestInverseResolution:
    def test_inverse_field_returns_pointing_entities(self, built, store):
        data = _run(
            built[0],
            store,
            '{ customer(id: "CUST00174") { id salesTransactionsViaSoldTo { id } } }',
        )
        assert _ids(data["customer"]["salesTransactionsViaSoldTo"]) == [
            "SAL001",
            "SAL002",
        ]

    def test_inverse_field_nested_forward_fields(self, built, store):
        data = _run(
            built[0],
            store,
            '{ customer(id: "CUST00370") { salesTransactionsViaSoldTo '
            "{ ofProduct { label } } } }",
        )
        sales = data["customer"]["salesTransactionsViaSoldTo"]
        assert sales[0]["ofProduct"][0]["label"] == "VisionTV G4 65"

    def test_inverse_field_absent_when_no_link(self, built, store):
        store.add(f"{D}Customer/CUST09999", RDF_TYPE, f"{B}Customer")
        data = _run(
            built[0],
            store,
            '{ customer(id: "CUST09999") { salesTransactionsViaSoldTo { id } } }',
        )
        assert data["customer"]["salesTransactionsViaSoldTo"] is None


# ---------------------------------------------------------------------------
# Filters and counts
# ---------------------------------------------------------------------------


class TestFilters:
    def test_relationship_filter_matches_target_label(self, built, store):
        data = _run(
            built[0],
            store,
            '{ salesTransactions(filters: [{field: "soldTo", value: "홍길동"}]) { id } }',
        )
        assert _ids(data["salesTransactions"]) == ["SAL001", "SAL002", "SAL003"]

    def test_relationship_filter_matches_target_id(self, built, store):
        data = _run(
            built[0],
            store,
            '{ salesTransactions(filters: [{field: "soldTo", value: "CUST00174"}]) '
            "{ id } }",
        )
        assert _ids(data["salesTransactions"]) == ["SAL001", "SAL002"]

    def test_data_filter_is_case_insensitive_contains(self, built, store):
        data = _run(
            built[0],
            store,
            '{ customers(filters: [{field: "customerId", value: "cust0017"}]) { id } }',
        )
        assert _ids(data["customers"]) == ["CUST00174"]

    def test_id_filter_matches_exactly(self, built, store):
        data = _run(
            built[0],
            store,
            '{ customers(filters: [{field: "id", value: "CUST00370"}]) { id } }',
        )
        assert _ids(data["customers"]) == ["CUST00370"]

    def test_filters_are_and_combined(self, built, store):
        data = _run(
            built[0],
            store,
            '{ salesTransactions(filters: [{field: "bySalesOrg", value: "중앙"},'
            ' {field: "ofProduct", value: "VisionTV"}]) { id } }',
        )
        assert _ids(data["salesTransactions"]) == [
            "SAL001",
            "SAL003",
            "SAL004",
            "SAL005",
        ]

    def test_unknown_filter_field_lists_valid_fields(self, built, store):
        result = built[0].execute_sync(
            '{ salesTransactions(filters: [{field: "bogus", value: "x"}]) { id } }',
            context_value={"triplestore": store, "table_name": TABLE},
        )
        assert "Valid fields" in str(result.errors[0])

    def test_limit_is_clamped_to_ceiling(self, built, store):
        _run(built[0], store, "{ salesTransactions(limit: 999999) { id } }")
        assert f"LIMIT {MAX_LIST_LIMIT} " in store.queries[0]


class TestCounts:
    _FILTERS = (
        '[{field: "bySalesOrg", value: "중앙"}, {field: "ofProduct", value: "VisionTV"}]'
    )

    def test_count_with_filters(self, built, store):
        data = _run(
            built[0], store, f"{{ salesTransactionsCount(filters: {self._FILTERS}) }}"
        )
        assert data["salesTransactionsCount"] == 4

    def test_count_distinct_relationship_targets(self, built, store):
        data = _run(
            built[0],
            store,
            f'{{ salesTransactionsCount(filters: {self._FILTERS}, distinctBy: "soldTo") }}',
        )
        assert data["salesTransactionsCount"] == 3

    def test_count_without_filters(self, built, store):
        data = _run(built[0], store, "{ customersCount }")
        assert data["customersCount"] == 3

    def test_count_with_search(self, built, store):
        data = _run(built[0], store, '{ customersCount(search: "홍길동") }')
        assert data["customersCount"] == 2


# ---------------------------------------------------------------------------
# Selection-aware resolution
# ---------------------------------------------------------------------------


class TestSelectionAwareResolution:
    def test_scalar_only_query_loads_root_triples_once(self, built, store):
        _run(built[0], store, "{ salesTransactions { id saleDate } }")
        assert len(store.loaded_subjects) == 1

    def test_unselected_relationship_targets_are_not_loaded(self, built, store):
        _run(built[0], store, "{ salesTransactions { id soldTo { label } } }")
        loaded = {u for batch in store.loaded_subjects for u in batch}
        assert not any("/Product/" in u for u in loaded)

    def test_identity_only_nested_selection_skips_triple_load(self, built, store):
        _run(built[0], store, "{ salesTransactions { id soldTo { id } } }")
        assert len(store.loaded_subjects) == 1

    def test_unselected_inverse_field_runs_no_reverse_lookup(self, built, store):
        _run(built[0], store, "{ customers { id label } }")
        assert not any("object IN" in q for q in store.queries)

    def test_fragment_selection_is_honoured(self, built, store):
        data = _run(
            built[0],
            store,
            "{ salesTransactions(limit: 1) { ...F } } "
            "fragment F on SalesTransaction { id ofProduct { label } }",
        )
        assert data["salesTransactions"][0]["ofProduct"][0]["label"]

    def test_legacy_resolution_without_selection_is_eager(self, built, store):
        metadata = built[1]
        rows = metadata.resolve_list(store, TABLE, "SalesTransaction", limit=1)
        assert rows[0].ofProduct and rows[0].soldTo

    def test_legacy_resolution_leaves_inverse_fields_unset(self, built, store):
        metadata = built[1]
        rows = metadata.resolve_list(store, TABLE, "Customer", limit=1)
        assert rows[0].salesTransactionsViaSoldTo is None


# ---------------------------------------------------------------------------
# The two production questions, end to end (schema + SQL + formatter)
# ---------------------------------------------------------------------------


class TestProductionQuestions:
    def test_purchases_of_customer_with_product_and_channel(self, built, store):
        data = _run(
            built[0],
            store,
            '{ salesTransactions(filters: [{field: "soldTo", value: "홍길동"}], '
            "limit: 200) { id soldTo { id label } ofProduct { label } "
            "viaChannel { label } } }",
        )
        by_customer: Dict[str, List[str]] = {}
        for row in data["salesTransactions"]:
            by_customer.setdefault(row["soldTo"][0]["id"], []).append(
                row["ofProduct"][0]["label"]
            )
        assert by_customer == {
            "CUST00174": ["VisionTV C4 55", "AirPro 에어컨"],
            "CUST00370": ["VisionTV G4 65"],
        }

    def test_purchases_via_inverse_field(self, built, store):
        data = _run(
            built[0],
            store,
            '{ customers(filters: [{field: "label", value: "홍길동"}]) '
            "{ id salesTransactionsViaSoldTo { id viaChannel { label } } } }",
        )
        counts = {
            c["id"]: len(c["salesTransactionsViaSoldTo"]) for c in data["customers"]
        }
        assert counts == {"CUST00174": 2, "CUST00370": 1}

    def test_gangnam_oled_buyers_rows_and_counts(self, built, store):
        filters = (
            '[{field: "bySalesOrg", value: "중앙"}, {field: "ofProduct", value: "VisionTV"}]'
        )
        data = _run(
            built[0],
            store,
            f"{{ rows: salesTransactions(filters: {filters}, limit: 500) "
            f"{{ id soldTo {{ id label }} ofProduct {{ label }} }} "
            f"total: salesTransactionsCount(filters: {filters}) "
            f'buyers: salesTransactionsCount(filters: {filters}, distinctBy: "soldTo") }}',
        )
        assert (len(data["rows"]), data["total"], data["buyers"]) == (4, 4, 3)

    def test_formatter_reports_rows_and_distinct_targets(self, built, store):
        data = _run(
            built[0],
            store,
            '{ salesTransactions(filters: [{field: "bySalesOrg", value: "중앙"},'
            ' {field: "ofProduct", value: "VisionTV"}]) { id soldTo { id label } } }',
        )
        text = format_graphql_response({"data": data}, "lge_test")
        assert "salesTransactions: 4 rows" in text and "distinct soldTo: 3" in text


def _fact_rows(n: int, with_uri: bool) -> List[Dict[str, Any]]:
    rows = []
    for i in range(n):
        customer = {"id": f"CUST{i:05d}", "label": "이몽룡"}
        if with_uri:
            customer["uri"] = f"https://databricks-ontology.com/Demo/Customer/CUST{i:05d}"
        rows.append(
            {
                "id": f"SAL{i:05d}",
                "saleDate": "2024-11-28",
                "grossKrw": "3483015",
                "soldTo": [customer],
                "ofProduct": [{"label": "VisionTV C4 55 2025형"}],
                "bookedBy": [{"label": "중앙지점"}],
            }
        )
    return rows


class TestFormatterCompactness:
    def test_110_row_fact_list_fits_default_tool_cap(self):
        from agents.agent_dtwin_chat.tools import _MAX_TOOL_RESULT_CHARS

        text = format_graphql_response(
            {"data": {"salesTransactions": _fact_rows(110, with_uri=False)}}, "Demo"
        )
        assert len(text) < _MAX_TOOL_RESULT_CHARS

    def test_110_row_fact_list_with_uris_fits_graphql_cap(self):
        from agents.agent_dtwin_chat.tools import _MAX_GRAPHQL_RESULT_CHARS

        text = format_graphql_response(
            {"data": {"salesTransactions": _fact_rows(110, with_uri=True)}}, "Demo"
        )
        assert len(text) < _MAX_GRAPHQL_RESULT_CHARS

    def test_single_entity_reference_is_inlined(self):
        text = format_graphql_response(
            {"data": {"salesTransactions": _fact_rows(1, with_uri=False)}}, "Demo"
        )
        assert "soldTo=이몽룡 [CUST00000]" in text

    def test_long_nested_lists_become_sub_rows(self):
        customer = {
            "id": "CUST1",
            "label": "A",
            "salesTransactionsViaSoldTo": [{"id": f"S{i}"} for i in range(5)],
        }
        text = format_graphql_response({"data": {"customer": customer}}, "Demo")
        assert "salesTransactionsViaSoldTo (5):" in text


class TestSchemaDigest:
    def test_digest_lists_root_fields(self, built):
        assert "[salesTransactions, salesTransactionsCount, salesTransaction]" in (
            built[1].digest()
        )

    def test_digest_lists_inverse_fields(self, built):
        assert "salesTransactionsViaSoldTo<-SalesTransaction" in built[1].digest()

    def test_digest_includes_relationship_labels(self, built):
        assert 'soldTo->Customer "Buyer"' in built[1].digest()

    def test_digest_is_memoised(self, built):
        assert built[1].digest() is built[1].digest()

    def test_digest_respects_size_cap(self):
        from back.core.graphql import SchemaDigest

        classes = [
            {
                "uri": f"{B}Type{i}",
                "name": f"Type{i}",
                "dataProperties": [
                    {"name": f"attribute{i}_{j}", "uri": f"{B}attribute{i}_{j}"}
                    for j in range(30)
                ],
            }
            for i in range(40)
        ]
        _, metadata = GraphQLSchemaBuilder().build_for_domain(classes, [], B, "big")
        assert len(SchemaDigest.render(metadata, max_chars=3000)) <= 3000


class TestChatSchemaDigestHelper:
    def test_returns_digest_for_session_domain(self):
        from types import SimpleNamespace

        from api.routers.internal import dtwin

        domain = SimpleNamespace(
            ontology={"classes": CLASSES, "properties": PROPERTIES, "base_uri": B},
            info={"name": "digest_helper_domain"},
        )
        assert "salesTransactionsViaSoldTo" in dtwin._chat_schema_digest(domain)

    def test_empty_ontology_returns_empty_digest(self):
        from types import SimpleNamespace

        from api.routers.internal import dtwin

        domain = SimpleNamespace(ontology={}, info={"name": "empty"})
        assert dtwin._chat_schema_digest(domain) == ""
