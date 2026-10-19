"""Tests for agents.tracing – MLflow tracing helpers."""

import pytest
from unittest.mock import patch, MagicMock

import agents.tracing as tracing_mod
from agents.tracing import (
    _resolve_experiment_name,
    _safe_inputs,
    _safe_result,
    _truncate,
    trace_agent,
    trace_llm,
    trace_tool,
    is_tracing_ready,
    setup_tracing,
)


class TestResolveExperimentName:
    def test_absolute_path_unchanged(self, monkeypatch):
        monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
        assert _resolve_experiment_name("/Users/me/exp") == "/Users/me/exp"

    def test_local_tracking_unchanged(self, monkeypatch):
        monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
        assert _resolve_experiment_name("my-experiment") == "my-experiment"

    def test_databricks_tracking_adds_shared(self, monkeypatch):
        monkeypatch.setenv("MLFLOW_TRACKING_URI", "databricks")
        assert _resolve_experiment_name("my-exp") == "/Shared/my-exp"

    def test_databricks_tracking_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("MLFLOW_TRACKING_URI", "Databricks")
        assert _resolve_experiment_name("my-exp") == "/Shared/my-exp"

    def test_non_databricks_tracking(self, monkeypatch):
        monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://mlflow:5000")
        assert _resolve_experiment_name("my-exp") == "my-exp"


class TestTruncate:
    def test_short_string(self):
        assert _truncate("hello", 10) == "hello"

    def test_exact_limit(self):
        assert _truncate("hello", 5) == "hello"

    def test_over_limit(self):
        result = _truncate("hello world", 5)
        assert result == "hello…"
        assert len(result) == 6  # 5 chars + ellipsis


class TestSafeInputs:
    def test_excludes_secrets(self):
        result = _safe_inputs({"token": "secret", "host": "https://h", "client": "obj", "name": "ok"})
        assert "token" not in result
        assert "host" not in result
        assert "client" not in result
        assert result["name"] == "ok"

    def test_truncates_strings(self):
        result = _safe_inputs({"data": "x" * 500})
        assert result["data"].endswith("…")

    def test_lists_summarised(self):
        result = _safe_inputs({"items": [1, 2, 3]})
        assert "list" in result["items"]
        assert "3" in result["items"]

    def test_dicts_summarised(self):
        result = _safe_inputs({"cfg": {"a": 1, "b": 2}})
        assert "dict" in result["cfg"]

    def test_other_types(self):
        result = _safe_inputs({"obj": object()})
        assert result["obj"] == "object"

    def test_primitives(self):
        result = _safe_inputs({"n": 42, "f": 3.14, "b": True, "none": None})
        assert result["n"] == "42"
        assert result["b"] == "True"


class TestSafeResult:
    def test_none_result(self):
        assert _safe_result(None) == {}

    def test_agent_result_like(self):
        class FakeResult:
            success = True
            iterations = 3
            error = None
            usage = {"prompt_tokens": 10}

        result = _safe_result(FakeResult())
        assert result["success"] is True
        assert result["iterations"] == 3
        assert "error" not in result

    def test_plain_object(self):
        assert _safe_result(object()) == {}


class TestSetupTracing:
    def test_success(self, monkeypatch):
        tracing_mod._TRACING_READY = False
        mock_mlflow = MagicMock()

        with patch.dict("sys.modules", {"mlflow": mock_mlflow}):
            result = setup_tracing("test-exp")

        assert result is True
        assert tracing_mod._TRACING_READY is True

    def test_failure_graceful(self, monkeypatch):
        tracing_mod._TRACING_READY = True

        with patch.dict("sys.modules", {"mlflow": None}):
            with patch("builtins.__import__", side_effect=ImportError("no mlflow")):
                result = setup_tracing()

        assert result is False
        assert tracing_mod._TRACING_READY is False

    def test_is_tracing_ready_reflects_state(self):
        tracing_mod._TRACING_READY = False
        assert is_tracing_ready() is False
        tracing_mod._TRACING_READY = True
        assert is_tracing_ready() is True
        tracing_mod._TRACING_READY = False


class TestTraceDecorators:
    """When tracing is NOT ready, decorators should be no-ops."""

    def setup_method(self):
        tracing_mod._TRACING_READY = False

    def test_trace_agent_passthrough(self):
        @trace_agent("test")
        def my_agent(**kwargs):
            return "result"

        assert my_agent() == "result"

    def test_trace_llm_passthrough(self):
        @trace_llm("test")
        def my_llm(*args, **kwargs):
            return {"choices": [{"message": {"content": "hi"}}]}

        assert my_llm()["choices"][0]["message"]["content"] == "hi"

    def test_trace_tool_passthrough(self):
        @trace_tool("test")
        def my_dispatch(*args, **kwargs):
            return "tool result"

        assert my_dispatch() == "tool result"

    def test_preserves_function_name(self):
        @trace_agent()
        def original_name():
            pass

        assert original_name.__name__ == "original_name"


class TestTraceAgentStageTags:
    """When tracing IS ready, ``trace_agent`` tags the span with the staged
    identifiers (stage, draft_id, draft_revision) so a resumed run's traces
    correlate across substages (SPEC §8)."""

    def _fake_mlflow(self, recorded):
        class _Span:
            def set_inputs(self, *_a, **_k):
                pass

            def set_outputs(self, *_a, **_k):
                pass

            def set_attributes(self, attrs):
                recorded.update(attrs)

        class _CM:
            def __enter__(self_inner):
                return _Span()

            def __exit__(self_inner, *exc):
                return False

        mock = MagicMock()
        mock.start_span.return_value = _CM()

        class _SpanType:
            AGENT = "AGENT"
            LLM = "LLM"
            TOOL = "TOOL"

        entities = MagicMock()
        entities.SpanType = _SpanType
        return mock, entities

    def test_stage_and_draft_tags_set_on_span(self, monkeypatch):
        recorded: dict = {}
        mock_mlflow, mock_entities = self._fake_mlflow(recorded)
        tracing_mod._TRACING_READY = True
        with patch.dict(
            "sys.modules",
            {"mlflow": mock_mlflow, "mlflow.entities": mock_entities},
        ):
            @trace_agent(name="owl_generator.detect", stage="detect")
            def my_stage(**kwargs):
                return "ok"

            assert my_stage(draft_id="draft-1", draft_revision=4) == "ok"
        tracing_mod._TRACING_READY = False
        assert recorded.get("stage") == "detect"
        assert recorded.get("draft_id") == "draft-1"
        assert recorded.get("draft_revision") == "4"


class TestTraceToolStatus:
    """Review fix (item 2/3): ``trace_tool``'s span carries both the tool
    name (input) and an ok/error ``status`` (output) derived from the
    dispatched result, both surfaced as **span attributes** — MLflow's
    ``Span.set_attributes``/``set_inputs``/``set_outputs`` API, not trace-
    level ``tags`` (see the docstring fix in ``trace_agent``/``trace_tool``:
    this module never calls a trace-tags API)."""

    def _fake_mlflow(self):
        class _Span:
            def __init__(self):
                self.inputs = {}
                self.outputs = {}

            def set_inputs(self, data):
                self.inputs.update(data)

            def set_outputs(self, data):
                self.outputs.update(data)

        class _CM:
            def __init__(self, span):
                self._span = span

            def __enter__(self_inner):
                return self_inner._span

            def __exit__(self_inner, *exc):
                return False

        span = _Span()
        mock = MagicMock()
        mock.start_span.return_value = _CM(span)

        class _SpanType:
            AGENT = "AGENT"
            LLM = "LLM"
            TOOL = "TOOL"

        entities = MagicMock()
        entities.SpanType = _SpanType
        return mock, entities, span

    def test_status_ok_for_a_plain_result(self):
        mock_mlflow, mock_entities, span = self._fake_mlflow()
        tracing_mod._TRACING_READY = True
        try:
            with patch.dict(
                "sys.modules",
                {"mlflow": mock_mlflow, "mlflow.entities": mock_entities},
            ):
                @trace_tool()
                def dispatch(ctx, tool_name, arguments, *, trace_name="x"):
                    return '{"tables": []}'

                dispatch(None, "get_metadata", {})
        finally:
            tracing_mod._TRACING_READY = False
        assert span.inputs["tool_name"] == "get_metadata"
        assert span.outputs["status"] == "ok"

    def test_status_error_for_a_json_error_result(self):
        mock_mlflow, mock_entities, span = self._fake_mlflow()
        tracing_mod._TRACING_READY = True
        try:
            with patch.dict(
                "sys.modules",
                {"mlflow": mock_mlflow, "mlflow.entities": mock_entities},
            ):
                @trace_tool()
                def dispatch(ctx, tool_name, arguments, *, trace_name="x"):
                    return '{"error": "table_name is required"}'

                dispatch(None, "get_table_detail", {})
        finally:
            tracing_mod._TRACING_READY = False
        assert span.outputs["status"] == "error"

    def test_status_ok_for_a_non_json_string_result(self):
        mock_mlflow, mock_entities, span = self._fake_mlflow()
        tracing_mod._TRACING_READY = True
        try:
            with patch.dict(
                "sys.modules",
                {"mlflow": mock_mlflow, "mlflow.entities": mock_entities},
            ):
                @trace_tool()
                def dispatch(ctx, tool_name, arguments, *, trace_name="x"):
                    return "not json"

                dispatch(None, "some_tool", {})
        finally:
            tracing_mod._TRACING_READY = False
        assert span.outputs["status"] == "ok"


class TestPayloadSummaries:
    """Trace payload helpers used by the LLM / tool spans."""

    def test_reasoning_only_reply_has_no_text(self):
        result = {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "content": [
                            {"type": "reasoning", "summary": [{"signature": "SECRET"}]}
                        ]
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2048},
        }
        out = tracing_mod._summarize_llm_response(result)
        assert out["finish_reason"] == "length"
        assert out["content_block_types"] == ["reasoning"]
        assert out["text_length"] == 0

    def test_reasoning_signature_not_recorded(self):
        content = [
            {"type": "reasoning", "summary": [{"signature": "SECRET"}]},
            {"type": "text", "text": "answer"},
        ]
        text = tracing_mod._content_text(content)
        assert "SECRET" not in text
        assert "answer" in text

    def test_reply_text_recorded(self):
        result = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]}
        assert tracing_mod._summarize_llm_response(result)["text"] == "hello"

    def test_payloads_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_TRACE_PAYLOADS", "false")
        result = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]}
        assert "text" not in tracing_mod._summarize_llm_response(result)

    def test_messages_keep_tool_calls(self):
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "function": {"name": "describe_entity", "arguments": "{}"}}
                ],
            }
        ]
        out = tracing_mod._summarize_messages(msgs)
        assert out[0]["tool_calls"][0]["name"] == "describe_entity"

    def test_message_content_truncated(self, monkeypatch):
        monkeypatch.setattr(tracing_mod, "TRACE_MAX_CHARS", 10)
        out = tracing_mod._summarize_messages([{"role": "tool", "content": "x" * 50}])
        assert out[0]["content"] == "x" * 10 + "…"

    def test_safe_result_includes_reply(self):
        class FakeResult:
            success = True
            reply = "final answer"

        assert _safe_result(FakeResult())["reply"] == "final answer"


class TestBindToolCall:
    def test_dispatcher_signature(self):
        def dispatch_tool(handlers, ctx, tool_name, arguments, *, trace_name=""):
            return ""

        name, args = tracing_mod._bind_tool_call(
            dispatch_tool, "agent:tool", ({}, object(), "describe_entity", {"search": "x"}), {}
        )
        assert (name, args) == ("describe_entity", {"search": "x"})

    def test_plain_tool_drops_context(self):
        def tool_get(ctx, *, uri, **_kwargs):
            return ""

        name, args = tracing_mod._bind_tool_call(
            tool_get, "get_entity_details", (object(),), {"uri": "u1"}
        )
        assert (name, args) == ("get_entity_details", {"uri": "u1"})


class TestActiveSpans:
    """With tracing ready, spans record payloads."""

    def setup_method(self):
        tracing_mod._TRACING_READY = True

    def teardown_method(self):
        tracing_mod._TRACING_READY = False

    def _mlflow(self):
        mock_mlflow = MagicMock()
        span = MagicMock()
        mock_mlflow.start_span.return_value.__enter__.return_value = span
        return mock_mlflow, span

    def test_llm_span_records_messages_and_text(self):
        mock_mlflow, span = self._mlflow()

        @trace_llm("agent:llm")
        def call(host, token, endpoint_name, messages, *, tools=None, max_tokens=0):
            return {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}

        with patch.dict("sys.modules", {"mlflow": mock_mlflow, "mlflow.entities": MagicMock()}):
            call("h", "t", "ep", [{"role": "user", "content": "q"}], max_tokens=8192)

        inputs = span.set_inputs.call_args[0][0]
        outputs = span.set_outputs.call_args[0][0]
        assert inputs["messages"][0]["content"] == "q"
        assert inputs["max_tokens"] == 8192
        assert outputs["text"] == "hi"

    def test_tool_span_records_result(self):
        mock_mlflow, span = self._mlflow()

        @trace_tool("agent:tool")
        def dispatch_tool(handlers, ctx, tool_name, arguments):
            return "tool output"

        with patch.dict("sys.modules", {"mlflow": mock_mlflow, "mlflow.entities": MagicMock()}):
            dispatch_tool({}, None, "list_entity_types", {})

        assert mock_mlflow.start_span.call_args.kwargs["name"] == "tool:list_entity_types"
        assert span.set_outputs.call_args[0][0]["result"] == "tool output"


def test_graph_chat_tool_call_opens_a_tool_span_named_after_the_tool():
    from agents.agent_dtwin_chat import engine

    mock_mlflow, mock_entities, span = TestTraceToolStatus()._fake_mlflow()
    tracing_mod._TRACING_READY = True
    try:
        with patch.dict(
            "sys.modules",
            {"mlflow": mock_mlflow, "mlflow.entities": mock_entities},
        ), patch.object(engine, "dispatch_tool", return_value='{"types": []}'):
            assert engine._dispatch_chat_tool(None, "list_entity_types", {}) == '{"types": []}'
    finally:
        tracing_mod._TRACING_READY = False
    assert mock_mlflow.start_span.call_args.kwargs["name"] == "tool:list_entity_types"
    assert span.outputs["status"] == "ok"
