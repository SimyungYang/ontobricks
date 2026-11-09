"""derivedFrom stays correct when a class links several UC datasets (#221)."""

import pytest

from back.objects.mapping import MetricViewBindings
from back.objects.ontology.definitions import DefinitionReferences
from tests.units.definitions.conftest import (
    MV,
    SALES_URI,
    build_mapping,
    build_metadata,
    build_ontology,
)

pytestmark = pytest.mark.unit

REVENUE_KEY = f"{SALES_URI}|totalRevenue"
OTHER_MV = "main.sales.forecast_mv"
ORDERS = "main.sales.orders"


def _dataset(full_name, kind):
    catalog, schema, asset = full_name.split(".")
    return {"catalog": catalog, "schema": schema, "asset": asset, "fullName": full_name, "type": kind}


def _metadata_with_two_views():
    meta = build_metadata()
    meta["tables"].append(
        {
            "name": "forecast_mv",
            "full_name": OTHER_MV,
            "object_kind": "metric_view",
            "columns": [{"name": "total_revenue", "type": "double", "role": "measure"}],
        }
    )
    meta["tables"].append(
        {"name": "orders", "full_name": ORDERS, "object_kind": "table", "columns": [{"name": "total_revenue"}]}
    )
    return meta


def _two_view_mapping():
    return build_mapping(
        catalog="",
        schema="",
        table="",
        sql_query=(
            f"SELECT a.region, a.total_revenue FROM {OTHER_MV} a "
            f"JOIN {MV} b ON a.region = b.region"
        ),
    )


class TestLinkedDatasets:
    def test_reads_the_datasets_list(self):
        onto = build_ontology()
        onto["classes"][0]["datasets"] = [_dataset(ORDERS, "TABLE"), _dataset(MV, "METRIC_VIEW")]
        assert DefinitionReferences.linked_datasets(onto) == {SALES_URI: [ORDERS, MV]}

    def test_reads_the_legacy_singular_dataset(self):
        onto = build_ontology()
        onto["classes"][0]["dataset"] = _dataset(MV, "METRIC_VIEW")
        assert DefinitionReferences.linked_datasets(onto) == {SALES_URI: [MV]}

    def test_class_without_datasets_is_omitted(self):
        assert DefinitionReferences.linked_datasets(build_ontology()) == {}


class TestResolveWithLinkedDatasets:
    def test_mapping_on_a_metric_view_binds_with_table_and_view_linked(self):
        linked = {SALES_URI: [ORDERS, MV]}
        bindings = MetricViewBindings.resolve([build_mapping()], _metadata_with_two_views(), linked=linked)
        assert bindings[REVENUE_KEY] == {"view": MV, "column": "total_revenue", "role": "measure"}

    def test_linked_metric_view_not_read_by_the_mapping_is_not_bound(self):
        mapping = build_mapping(
            catalog="main", schema="sales", table="orders", sql_query=f"SELECT * FROM {ORDERS}"
        )
        linked = {SALES_URI: [ORDERS, MV]}
        assert MetricViewBindings.resolve([mapping], _metadata_with_two_views(), linked=linked) == {}

    def test_class_linked_view_wins_when_the_mapping_reads_two_views(self):
        linked = {SALES_URI: [ORDERS, MV]}
        bindings = MetricViewBindings.resolve([_two_view_mapping()], _metadata_with_two_views(), linked=linked)
        assert bindings[REVENUE_KEY]["view"] == MV

    def test_without_links_the_first_sql_source_wins(self):
        bindings = MetricViewBindings.resolve([_two_view_mapping()], _metadata_with_two_views())
        assert bindings[REVENUE_KEY]["view"] == OTHER_MV


class TestReconcileUsesClassDatasets:
    def test_reconcile_picks_the_view_linked_to_the_class(self):
        onto = build_ontology()
        onto["classes"][0]["datasets"] = [_dataset(MV, "METRIC_VIEW"), _dataset(ORDERS, "TABLE")]
        DefinitionReferences.reconcile(onto, [_two_view_mapping()], _metadata_with_two_views())
        item = DefinitionReferences.items(onto)[REVENUE_KEY]
        assert item["derived_from"]["view"] == MV
