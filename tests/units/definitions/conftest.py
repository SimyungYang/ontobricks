"""Builders for the UC definition-sync tests (metric views + UC Pages)."""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

import pytest

BASE = "http://test.org/onto#"
SALES_URI = BASE + "Sales"
MV = "main.sales.revenue_mv"

MV_DDL = """
CREATE VIEW `main`.`sales`.`revenue_mv`
WITH METRICS
LANGUAGE YAML
AS $$
version: 1.1
source: main.sales.orders
dimensions:
  - name: region
    expr: region_code
    comment: Sales region
measures:
  - name: total_revenue
    expr: SUM(amount)
    comment: Gross revenue in EUR
    synonyms: [revenue, turnover]
$$
"""


def build_ontology() -> Dict[str, Any]:
    return {
        "name": "sales",
        "base_uri": BASE,
        "classes": [
            {
                "uri": SALES_URI,
                "name": "Sales",
                "label": "Sales",
                "description": "User-written description",
                "alternate_labels": ["deal"],
                "dataProperties": [
                    {"name": "region", "localName": "region"},
                    {"name": "totalRevenue", "localName": "totalRevenue"},
                    {"name": "note", "localName": "note"},
                ],
            }
        ],
        "properties": [],
    }


def build_mapping(**over: Any) -> Dict[str, Any]:
    mapping = {
        "ontology_class": SALES_URI,
        "sql_query": f"SELECT region, MEASURE(total_revenue) AS total_revenue FROM {MV} GROUP BY region",
        "id_column": "region",
        "label_column": "region",
        "catalog": "main",
        "schema": "sales",
        "table": "revenue_mv",
        "attribute_mappings": {"totalRevenue": "total_revenue", "region": "region"},
    }
    mapping.update(over)
    return mapping


def build_metadata(object_kind: str = "metric_view") -> Dict[str, Any]:
    return {
        "tables": [
            {
                "name": "revenue_mv",
                "full_name": MV,
                "object_kind": object_kind,
                "columns": [
                    {"name": "region", "type": "string", "role": "dimension"},
                    {"name": "total_revenue", "type": "double", "role": "measure"},
                ],
            }
        ]
    }


class FakeMetricSource:
    """Stands in for DatabricksClient.get_metric_view_definition (no warehouse)."""

    def __init__(self, definitions: Optional[Dict[str, Dict[str, Any]]] = None, error=None):
        self.definitions = definitions if definitions is not None else {}
        self.error = error
        self.calls: List[tuple] = []

    def get_metric_view_definition(self, catalog, schema, name):
        self.calls.append((catalog, schema, name))
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.definitions)


@pytest.fixture
def metric_defs():
    from back.core.databricks.uc import MetricViewDefinition

    return MetricViewDefinition.parse(MV_DDL)
