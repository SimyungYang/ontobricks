"""MetricViewDefinition: metric-view YAML -> per-column expr / comment / synonyms."""

import pytest

from back.core.databricks.uc import MetricViewDefinition
from back.core.databricks.uc.UnityCatalog import UnityCatalog
from tests.units.definitions.conftest import MV_DDL

pytestmark = pytest.mark.unit


def test_measure_carries_expr_comment_and_synonyms():
    measure = MetricViewDefinition.parse(MV_DDL)["total_revenue"]
    assert (measure["role"], measure["expr"], measure["comment"], measure["synonyms"]) == (
        "measure",
        "SUM(amount)",
        "Gross revenue in EUR",
        ["revenue", "turnover"],
    )


def test_column_without_synonyms_gets_an_empty_list():
    assert MetricViewDefinition.parse(MV_DDL)["region"]["synonyms"] == []


def test_dimension_only_view_has_no_measures():
    ddl = "AS $$\nversion: 0.1\ndimensions:\n  - name: country\n    expr: country\n$$"
    parsed = MetricViewDefinition.parse(ddl)
    assert {n: c["role"] for n, c in parsed.items()} == {"country": "dimension"}


def test_bare_yaml_without_ddl_wrapper_is_accepted():
    parsed = MetricViewDefinition.parse("measures:\n  - name: m\n    expr: COUNT(1)\n")
    assert parsed["m"]["expr"] == "COUNT(1)"


def test_single_string_synonym_is_normalised_to_a_list():
    ddl = "measures:\n  - name: m\n    expr: COUNT(1)\n    synonyms: orders\n"
    assert MetricViewDefinition.parse(ddl)["m"]["synonyms"] == ["orders"]


def test_malformed_yaml_soft_fails_to_empty():
    assert MetricViewDefinition.parse("AS $$\nmeasures: [unclosed\n  - name: x\n$$") == {}


def test_non_metric_ddl_parses_to_empty():
    assert MetricViewDefinition.parse("CREATE TABLE cat.s.t (a INT)") == {}


def test_unity_catalog_measure_names_reuse_the_parser():
    assert UnityCatalog._parse_measure_names(MV_DDL) == {"total_revenue"}


def test_get_metric_view_definition_wraps_read_failures(monkeypatch):
    from back.core.errors import InfrastructureError

    uc = UnityCatalog(object())

    def _boom(catalog, schema, name):
        raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND")

    monkeypatch.setattr(uc, "_show_create_ddl", _boom)
    with pytest.raises(InfrastructureError):
        uc.get_metric_view_definition("main", "sales", "gone_mv")


def test_get_metric_view_definition_returns_parsed_columns(monkeypatch):
    uc = UnityCatalog(object())
    monkeypatch.setattr(uc, "_show_create_ddl", lambda c, s, n: MV_DDL)
    assert set(uc.get_metric_view_definition("main", "sales", "revenue_mv")) == {
        "region",
        "total_revenue",
    }
