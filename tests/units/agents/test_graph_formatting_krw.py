"""Deterministic KRW -> 억 rendering in the graph tool formatters."""

import pytest

from agents.tools.graph_formatting import (
    format_find_response,
    format_graphql_response,
    with_krw_unit,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Baseline replies wrote these as 1,144.3억 and 1,975억 (100x off).
        ("1144300000", "1144300000 (11.44억)"),
        ("19748000000", "19748000000 (197.48억)"),
        ("1409235", "1409235 (0.014억, 140.9만원)"),
    ],
)
def test_krw_field_gets_eok_rendering(raw, expected):
    assert with_krw_unit("grossKrw", raw) == expected


@pytest.mark.parametrize("name", ["netKrw", "creditLimitKrw", "gross_krw", "AMOUNT_KRW"])
def test_krw_suffix_detection_is_case_insensitive(name):
    assert "억" in with_krw_unit(name, "100000000")


def test_non_krw_field_is_unchanged():
    assert with_krw_unit("quantity", "1144300000") == "1144300000"


def test_non_numeric_krw_value_is_unchanged():
    assert with_krw_unit("grossKrw", "N/A") == "N/A"


def test_graphql_formatter_annotates_krw_scalars():
    data = {"data": {"orgPerformances": [{"id": "P1", "actualNetKrw": "1144300000"}]}}
    assert "actualNetKrw=1144300000 (11.44억)" in format_graphql_response(data, "d")


def test_describe_formatter_annotates_krw_attributes():
    uri = "https://ex.org/Demo/SalesSummary/S1"
    data = {
        "success": True,
        "triples": [
            {"subject": uri, "predicate": "https://ex.org/onto/totalRevenueKrw",
             "object": "19748000000"},
        ],
        "seed_count": 1,
        "seed_uris": [uri],
    }
    assert "totalRevenueKrw: 19748000000 (197.48억)" in format_find_response(data)
