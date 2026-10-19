"""Unit tests for ``agents.agent_dtwin_chat.analysis`` (analyze_graphql).

A fake GraphQL executor serves fixed rows so paging, grouping and set
operations are checked without network I/O.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from agents.agent_dtwin_chat import analysis
from agents.agent_dtwin_chat import tools as chat_tools
from agents.tools.context import ToolContext


def _cust(n: int) -> dict:
    return {"id": f"C{n}", "label": f"Cust {n}", "uri": f"http://x/Customer/C{n}"}


SALES = [
    {"id": "S1", "soldTo": _cust(1), "channel": {"id": "CH_A", "label": "Mall"}, "grossKrw": 300_000_000,
     "product": {"category": {"label": "TV"}}},
    {"id": "S2", "soldTo": _cust(1), "channel": {"id": "CH_B", "label": "Coupang"}, "grossKrw": 100_000_000,
     "product": {"category": {"label": "냉장고"}}},
    {"id": "S3", "soldTo": _cust(2), "channel": {"id": "CH_A", "label": "Mall"}, "grossKrw": 50_000_000,
     "product": {"category": {"label": "TV"}}},
    {"id": "S4", "soldTo": _cust(3), "channel": {"id": "CH_B", "label": "Coupang"}, "grossKrw": 20_000_000,
     "product": {"category": {"label": "냉장고"}}},
]
SUBS = [{"id": "B1", "status": "해지", "heldBy": _cust(2)}, {"id": "B2", "status": "구독중", "heldBy": _cust(3)}]


def _executor(tables: dict, calls: list | None = None, max_limit: int | None = None, fail_after: int | None = None):
    """Serve root lists honouring limit / offset literals in the query.

    *max_limit* clamps ``limit`` like the server does (``MAX_LIST_LIMIT``);
    *fail_after* returns a GraphQL error for every call after that many.
    """
    served = [0]

    def execute(query: str, _variables):
        if calls is not None:
            calls.append(query)
        served[0] += 1
        if fail_after is not None and served[0] > fail_after:
            return {"errors": [{"message": "page boom"}]}
        data = {}
        for alias, name, args in re.findall(r"(?:(\w+)\s*:\s*)?(\w+)\s*(\([^)]*\))?\s*\{", query):
            if name not in tables or (alias or name) in data:
                continue
            limit = re.search(r"limit\s*:\s*(\d+)", args or "")
            offset = re.search(r"offset\s*:\s*(\d+)", args or "")
            start = int(offset.group(1)) if offset else 0
            size = int(limit.group(1)) if limit else analysis.DEFAULT_LIST_LIMIT
            if max_limit is not None:
                size = min(size, max_limit)
            stop = start + size
            data[alias or name] = tables[name][start:stop]
        return {"data": data}

    return execute


class TestFetchAllRows:
    def test_pages_list_past_default_limit(self, monkeypatch):
        monkeypatch.setattr(analysis, "PAGE_SIZE", 3)
        monkeypatch.setattr(analysis, "DEFAULT_LIST_LIMIT", 2)
        rows = [{"id": f"R{i}"} for i in range(8)]
        calls: list = []
        lists, complete, _ = analysis.fetch_all_rows("{ r: rows { id } }", None, _executor({"rows": rows}, calls))
        assert [r["id"] for r in lists["r"]] == [f"R{i}" for i in range(8)]

    def test_marks_list_complete(self, monkeypatch):
        monkeypatch.setattr(analysis, "PAGE_SIZE", 3)
        _, complete, _ = analysis.fetch_all_rows(
            "{ rows(limit: 2) { id } }", None, _executor({"rows": [{"id": i} for i in range(5)]})
        )
        assert complete == {"rows": analysis.LIST_COMPLETE}

    def test_keeps_paging_when_server_clamps_requested_limit(self, monkeypatch):
        monkeypatch.setattr(analysis, "PAGE_SIZE", 3)
        monkeypatch.setattr(analysis, "MAX_LIST_LIMIT", 3)
        rows = [{"id": f"R{i}"} for i in range(8)]
        lists, status, _ = analysis.fetch_all_rows(
            "{ r: rows(limit: 5000) { id } }", None, _executor({"rows": rows}, max_limit=3)
        )
        assert (len(lists["r"]), status["r"]) == (8, analysis.LIST_COMPLETE)

    def test_page_error_is_reported_as_error_not_row_cap(self, monkeypatch):
        monkeypatch.setattr(analysis, "PAGE_SIZE", 2)
        monkeypatch.setattr(analysis, "DEFAULT_LIST_LIMIT", 2)
        out = analysis.analyze(
            "{ r: rows { id } }",
            _executor({"rows": [{"id": f"R{i}"} for i in range(6)]}, fail_after=1),
            operation="group",
            by=["id"],
        )
        assert "r=2 (PARTIAL: a page failed" in out and "row cap" not in out

    def test_row_cap_is_reported_as_row_cap(self, monkeypatch):
        monkeypatch.setattr(analysis, "PAGE_SIZE", 2)
        monkeypatch.setattr(analysis, "DEFAULT_LIST_LIMIT", 2)
        monkeypatch.setattr(analysis, "MAX_ROWS_PER_LIST", 4)
        out = analysis.analyze(
            "{ r: rows { id } }",
            _executor({"rows": [{"id": f"R{i}"} for i in range(6)]}),
            operation="group",
            by=["id"],
        )
        assert "r=4 (PARTIAL: row cap reached)" in out

    def test_executor_receives_callers_variables_object(self):
        received: list = []
        variables: dict = {}

        def execute(query, v):
            received.append(v)
            return {"data": {"r": []}}

        analysis.fetch_all_rows("{ r: rows { id } }", variables, execute)
        assert received[0] is variables

    def test_syntax_error_is_an_analysis_error(self):
        with pytest.raises(analysis.AnalysisError, match="GraphQL syntax error"):
            analysis.fetch_all_rows("{ r: rows { id ", None, _executor({}))

    def test_analysis_error_is_a_validation_error(self):
        from back.core.errors import ValidationError

        assert issubclass(analysis.AnalysisError, ValidationError)

    def test_page_query_keeps_only_used_variables(self):
        doc = analysis.parse("query Q($f: [FieldFilter!], $x: Int) { a: rows(filters: $f) { id } n: rowsCount }")
        op = doc.definitions[0]
        text = analysis._page_query(op, op.selection_set.selections[0], [], limit=10, offset=20)
        assert "$f" in text and "$x" not in text and "offset: 20" in text

    def test_rejects_mutations(self):
        with pytest.raises(analysis.AnalysisError):
            analysis.fetch_all_rows("mutation { x }", None, _executor({}))


class TestGroup:
    def _run(self, **kwargs):
        return analysis.analyze("{ s: sales { id } }", _executor({"sales": SALES}), operation="group", **kwargs)

    def test_count_shows_ties_with_shared_rank(self):
        out = self._run(by=["channel"])
        assert "1 (tie) | Mall [CH_A] | 2 | 2" in out and "1 (tie) | Coupang [CH_B] | 2 | 2" in out

    def test_sum_of_krw_field_has_eok_text(self):
        out = self._run(by=["soldTo"], metric="sum", field="grossKrw")
        assert "Cust 1 [C1] <http://x/Customer/C1> | 400,000,000 (4.00억) | 2" in out

    def test_count_distinct_nested_field(self):
        out = self._run(by=["soldTo.id"], metric="count_distinct", field="channel.id")
        assert out.splitlines()[4].startswith("1 | Cust 1 [C1]")

    def test_where_on_nested_path(self):
        out = self._run(by=["channel"], where=["product.category.label=TV"])
        assert "2 rows" in out and "| Mall [CH_A] | 2 |" in out and "Coupang" not in out

    def test_restrict_to_other_list_key_set(self):
        out = analysis.analyze(
            "{ s: sales { id } c: subs { id } }",
            _executor({"sales": SALES, "subs": SUBS}),
            operation="group",
            source="s",
            by=["channel"],
            restrict={"key": "soldTo.id", "in": {"source": "c", "key": "heldBy.id", "where": ["status=해지"]}},
        )
        assert "1 rows" in out and "| Mall [CH_A] | 1 |" in out

    def test_restrict_exclude_keeps_rows_outside_key_set(self):
        out = analysis.analyze(
            "{ s: sales { id } c: subs { id } }",
            _executor({"sales": SALES, "subs": SUBS}),
            operation="group",
            source="s",
            by=["soldTo"],
            restrict={"key": "soldTo.id", "in": {"source": "c", "key": "heldBy.id"}, "exclude": True},
        )
        assert "NOT in c.heldBy.id (2 keys): 2 rows" in out and "| Cust 1 [C1] <http://x/Customer/C1> | 2 | 2" in out

    def test_restrict_compare_exclude_keeps_non_matching_rows(self):
        reviews = [{"id": "R1", "reviewDate": "2025-01-10", "writtenBy": _cust(1)}]
        sales = [{"id": "S1", "saleDate": "2025-02-01", "soldTo": _cust(1)},
                 {"id": "S2", "saleDate": "2025-01-01", "soldTo": _cust(1)},
                 {"id": "S3", "saleDate": "2025-03-01", "soldTo": _cust(2)}]
        out = analysis.analyze(
            "{ s: sales { id } r: reviews { id } }",
            _executor({"sales": sales, "reviews": reviews}),
            operation="group",
            source="s",
            by=["id"],
            restrict={"key": "soldTo.id", "in": {"source": "r", "key": "writtenBy.id"},
                      "compare": "saleDate>reviewDate", "exclude": True},
        )
        assert "NOT matching r.writtenBy.id with saleDate > other.reviewDate (1 keys): 2 rows" in out
        assert "| S2 |" in out and "| S3 |" in out and "| S1 |" not in out

    def test_zero_match_lists_values_present(self):
        out = self._run(by=["channel"], where=["channel=Naver"])
        assert "values present -> channel: Mall [CH_A], Coupang [CH_B]" in out

    def test_restrict_compare_across_records(self):
        reviews = [{"id": "R1", "reviewDate": "2025-01-10", "writtenBy": _cust(1)},
                   {"id": "R2", "reviewDate": "2025-12-01", "writtenBy": _cust(2)}]
        sales = [{"id": "S1", "saleDate": "2025-02-01", "soldTo": _cust(1)},
                 {"id": "S2", "saleDate": "2025-03-01", "soldTo": _cust(2)},
                 {"id": "S3", "saleDate": "2025-01-01", "soldTo": _cust(1)}]
        out = analysis.analyze(
            "{ s: sales { id } r: reviews { id } }",
            _executor({"sales": sales, "reviews": reviews}),
            operation="group",
            source="s",
            by=["soldTo"],
            restrict={"key": "soldTo.id", "in": {"source": "r", "key": "writtenBy.id"},
                      "compare": "saleDate>reviewDate"},
        )
        assert "1 rows" in out and "groups: 1 " in out and "Cust 1 [C1]" in out

    def test_more_groups_note_uses_clamped_top(self, monkeypatch):
        monkeypatch.setattr(analysis, "MAX_TOP", 1)
        lines = analysis.group_rows(SALES, ["soldTo"], top=500)
        assert lines[-1] == "... 2 more groups (raise 'top' to see them)"

    def test_asc_order_ranks_smallest_first(self):
        lines = analysis.group_rows(SALES, ["soldTo"], metric="sum", field="grossKrw", order="asc")
        assert lines[1].startswith("1 | Cust 3 [C3]")

    def test_unknown_metric_is_an_error(self):
        with pytest.raises(analysis.AnalysisError):
            self._run(by=["channel"], metric="median", field="grossKrw")


class TestSetOperations:
    def _run(self, operation, sets):
        return analysis.analyze(
            "{ s: sales { id } c: subs { id } }",
            _executor({"sales": SALES, "subs": SUBS}),
            operation=operation,
            sets=sets,
        )

    def test_intersect_buyers_of_two_categories(self):
        out = self._run(
            "intersect",
            [
                {"source": "s", "key": "soldTo.id", "where": ["product.category.label=TV"]},
                {"source": "s", "key": "soldTo.id", "where": ["product.category.label=냉장고"]},
            ],
        )
        assert "RESULT intersect: 1 distinct" in out and "- Cust 1 [C1]" in out

    def test_difference_buyers_without_subscription(self):
        out = self._run("difference", [{"source": "s", "key": "soldTo.id"}, {"source": "c", "key": "heldBy.id"}])
        assert "RESULT difference: 1 distinct" in out and "- Cust 1 [C1]" in out

    def test_empty_set_lists_values_present(self):
        out = self._run("intersect", [{"source": "c", "key": "heldBy.id", "where": ["status=cancelled"]},
                                      {"source": "s", "key": "soldTo.id"}])
        assert "values present -> status: 해지, 구독중" in out

    def test_needs_two_sets(self):
        with pytest.raises(analysis.AnalysisError):
            self._run("union", [{"source": "s", "key": "soldTo.id"}])


class TestConditions:
    @pytest.mark.parametrize(
        "cond, expected",
        [("grossKrw>60000000", ["S1", "S2"]), ("channel!=Mall", ["S2", "S4"]), ("channel~coup", ["S2", "S4"])],
    )
    def test_condition_operators(self, cond, expected):
        conditions = [analysis.parse_condition(cond)]
        assert [r["id"] for r in SALES if analysis.row_matches(r, conditions)] == expected

    @pytest.mark.parametrize("op", [">", "<", ">=", "<="])
    def test_ordering_on_entity_without_label_id_or_uri_is_no_match(self, op):
        assert analysis.row_matches({"x": {"other": 1}}, [analysis.parse_condition(f"x{op}3")]) is False

    def test_not_equal_holds_when_no_value_equals_target(self):
        row = {"tags": ["a", "b"]}
        assert analysis.row_matches(row, [analysis.parse_condition("tags!=c")]) is True

    def test_not_equal_fails_when_any_value_equals_target(self):
        row = {"tags": ["a", "b"]}
        assert analysis.row_matches(row, [analysis.parse_condition("tags!=b")]) is False

    def test_field_to_field_comparison(self):
        rows = [{"id": "D1", "actual": "2025-01-05", "planned": "2025-01-03"},
                {"id": "D2", "actual": "2025-01-02", "planned": "2025-01-03"}]
        conditions = [analysis.parse_condition("actual>@planned")]
        assert [r["id"] for r in rows if analysis.row_matches(r, conditions)] == ["D1"]

    def test_set_having_keeps_keys_over_threshold(self):
        out = analysis.analyze(
            "{ s: sales { id } }",
            _executor({"sales": SALES}),
            operation="union",
            sets=[{"source": "s", "key": "soldTo.id", "having": "sum(grossKrw)>=100000000"},
                  {"source": "s", "key": "soldTo.id", "having": "count>=5"}],
        )
        assert "RESULT union: 1 distinct" in out and "- Cust 1 [C1]" in out

    def test_set_name_describes_where_and_having(self):
        name, members = analysis.key_set(
            {"s": SALES}, {"source": "s", "key": "soldTo.id", "where": ["channel=Mall"], "having": "count>=2"}
        )
        assert name == "s.soldTo.id where channel=Mall having count>=2" and list(members) == []

    def test_bad_having_is_an_error(self):
        with pytest.raises(analysis.AnalysisError):
            analysis.parse_having("sum>=3")

    def test_bad_condition_is_an_error(self):
        with pytest.raises(analysis.AnalysisError):
            analysis.parse_condition("no operator here")


class TestToolHandler:
    def _ctx(self) -> ToolContext:
        return ToolContext(
            host="https://test.databricks.com",
            token="t",
            dtwin_base_url="http://loopback.invalid",
            dtwin_domain_name="sales_demo",
        )

    def _patch(self, monkeypatch, handler):
        monkeypatch.setattr(
            chat_tools,
            "_client",
            lambda ctx: httpx.Client(base_url=ctx.dtwin_base_url, transport=httpx.MockTransport(handler)),
        )

    def test_handler_accepts_json_string_arguments(self, monkeypatch):
        execute = _executor({"sales": SALES})
        self._patch(monkeypatch, lambda req: httpx.Response(200, json=execute(json.loads(req.content)["query"], None)))
        out = chat_tools.tool_analyze_graphql(
            self._ctx(), query="{ s: sales { id } }", operation="group", by='["channel"]', where='["grossKrw>60000000"]'
        )
        assert "| Mall [CH_A] | 1 |" in out and "| Coupang [CH_B] | 1 |" in out

    def test_handler_reports_http_errors(self, monkeypatch):
        self._patch(monkeypatch, lambda req: httpx.Response(500, text="boom"))
        out = chat_tools.tool_analyze_graphql(self._ctx(), query="{ s: sales { id } }", operation="group", by=["x"])
        assert "GraphQL query failed (500)" in json.loads(out)["error"]

    def test_handler_reports_analysis_errors(self, monkeypatch):
        self._patch(monkeypatch, lambda req: httpx.Response(200, json={"data": {"s": []}}))
        out = chat_tools.tool_analyze_graphql(self._ctx(), query="{ s: sales { id } }", operation="group")
        assert "needs 'by'" in json.loads(out)["error"]

    def test_handler_fills_defaults_from_analysis_module(self, monkeypatch):
        captured: dict = {}

        def fake_analyze(query, execute, **kwargs):
            captured.update(kwargs)
            return "ok"

        monkeypatch.setattr(chat_tools, "analyze", fake_analyze)
        self._patch(monkeypatch, lambda req: httpx.Response(200, json={"data": {}}))
        chat_tools.tool_analyze_graphql(
            self._ctx(), query="{ s: sales { id } }", operation="group", by='"channel"',
            metric=None, top=None, order=None,
        )
        assert (captured["by"], captured["metric"], captured["top"], captured["order"]) == (
            ["channel"], analysis.DEFAULT_METRIC, analysis.DEFAULT_TOP, analysis.DEFAULT_ORDER,
        )

    def test_handler_rejects_bad_json_argument(self, monkeypatch):
        self._patch(monkeypatch, lambda req: httpx.Response(200, json={"data": {}}))
        out = chat_tools.tool_analyze_graphql(self._ctx(), query="{ s: sales { id } }", operation="group", by="[")
        assert json.loads(out)["error"] == "Invalid JSON in 'by' argument."

    def test_graphql_executor_posts_query_and_variables(self):
        bodies: list = []

        def handler(req):
            bodies.append(json.loads(req.content))
            return httpx.Response(200, json={"data": {"a": 1}})

        with httpx.Client(base_url="http://loopback.invalid", transport=httpx.MockTransport(handler)) as client:
            execute = chat_tools._graphql_executor(client)
            assert execute("{ a }", {"x": 1}) == {"data": {"a": 1}}
            execute("{ a }", None)
        assert bodies == [{"query": "{ a }", "variables": {"x": 1}}, {"query": "{ a }"}]


class TestAnalyzeToolDefinition:
    def _params(self) -> dict:
        return chat_tools._ANALYZE_GRAPHQL_DEF["function"]["parameters"]["properties"]

    def test_operation_enum_matches_analysis_constants(self):
        assert self._params()["operation"]["enum"] == ["group", *analysis.SET_OPERATIONS]

    def test_metric_enum_matches_analysis_constants(self):
        assert self._params()["metric"]["enum"] == list(analysis.METRICS)

    def test_selection_where_is_described(self):
        assert chat_tools._SELECTION_SCHEMA["properties"]["where"].get("description")
