"""Unit tests for Graph Chat final-answer normalization."""

import json
from unittest.mock import patch

import pytest

from agents.agent_dtwin_chat.engine import AgentResult, normalize_reply_content, run_agent
from agents.tools.context import ToolContext

_PENDING_ACTION = {
    "token": "tok",
    "entity_uri": "https://ex/Customer/CUST1",
    "entity_label": "CUST1",
    "action": "main.ops.recompute_risk",
    "description": "Risk",
    "expires_in_sec": 120,
}


def _tool_call_response():
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "tc1",
                            "function": {
                                "name": "request_entity_action",
                                "arguments": json.dumps(
                                    {
                                        "entity_uri": "https://ex/Customer/CUST1",
                                        "action": "main.ops.recompute_risk",
                                    }
                                ),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _fake_dispatch(_handlers, ctx, _tool_name, _arguments, *, trace_name=""):
    ctx.pending_action = _PENDING_ACTION
    return '{"success": true}'


def _run_with_scripted_llm(llm_side_effect):
    with (
        patch(
            "agents.agent_dtwin_chat.engine.call_serving_endpoint",
            side_effect=llm_side_effect,
        ),
        patch(
            "agents.agent_dtwin_chat.engine.dispatch_tool",
            side_effect=_fake_dispatch,
        ),
    ):
        return run_agent(
            host="https://example.invalid",
            token="token",
            endpoint_name="endpoint",
            base_url="http://localhost:8000",
            domain_name="main",
            registry_params={},
            session_cookies={},
            user_message="recompute risk",
        )


def test_run_agent_preserves_pending_action_on_llm_error():
    def llm_side_effect(*_args, **_kwargs):
        if llm_side_effect.calls == 0:
            llm_side_effect.calls += 1
            return _tool_call_response()
        raise RuntimeError("LLM unavailable")

    llm_side_effect.calls = 0
    result = _run_with_scripted_llm(llm_side_effect)

    assert result.success is False
    assert "LLM request failed" in result.error
    assert result.pending_action == _PENDING_ACTION


def test_run_agent_preserves_pending_action_on_empty_choices():
    def llm_side_effect(*_args, **_kwargs):
        if llm_side_effect.calls == 0:
            llm_side_effect.calls += 1
            return _tool_call_response()
        return {"choices": [], "usage": {}}

    llm_side_effect.calls = 0
    result = _run_with_scripted_llm(llm_side_effect)

    assert result.success is False
    assert result.error == "No choices in LLM response"
    assert result.pending_action == _PENDING_ACTION


def test_agent_result_and_context_carry_pending_action():
    ctx = ToolContext(host="h", token="t")
    assert ctx.dtwin_class_actions == {}
    assert ctx.pending_action is None
    result = AgentResult(success=True, pending_action={"token": "abc"})
    assert result.pending_action["token"] == "abc"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("A readable answer.", "A readable answer."),
        (
            [
                {"type": "text", "text": "First paragraph."},
                {"type": "text", "text": "Second paragraph."},
            ],
            "First paragraph.\n\nSecond paragraph.",
        ),
        (
            [
                {"type": "metadata", "data": {"internal": True}},
                {"type": "text", "text": "Visible answer."},
                {"type": "image", "url": "https://example.invalid/image"},
            ],
            "Visible answer.",
        ),
        (
            {"type": "text", "text": {"value": "Nested text."}},
            "Nested text.",
        ),
    ],
)
def test_normalize_reply_content_extracts_readable_text(content, expected):
    assert normalize_reply_content(content) == expected


@pytest.mark.parametrize("content", [None, "", [], {}, [{"type": "metadata"}]])
def test_normalize_reply_content_uses_non_technical_fallback(content):
    assert normalize_reply_content(content) == (
        "I couldn't display that answer. Please try again."
    )


def test_chat_turn_requests_reasoning_safe_token_budget():
    # Reasoning-capable models (Claude 4.6+/5.x) spend part of max_tokens on
    # hidden reasoning; 2048 left no room for the visible answer.
    seen = {}

    def llm_side_effect(*_args, **kwargs):
        seen["max_tokens"] = kwargs.get("max_tokens")
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {}}

    _run_with_scripted_llm(llm_side_effect)
    assert seen["max_tokens"] >= 8192


def test_unreadable_final_reply_is_logged_with_finish_reason():
    # A reasoning-only final message must leave a diagnosable warning in the
    # app log (finish_reason + block types), not just the UI fallback text.
    def llm_side_effect(*_args, **_kwargs):
        return {"choices": [{"message": {"role": "assistant",
                                         "content": [{"type": "reasoning", "summary": []}]},
                             "finish_reason": "length"}],
                "usage": {"completion_tokens": 8192}}

    with patch("agents.agent_dtwin_chat.engine.logger") as log:
        _run_with_scripted_llm(llm_side_effect)

    warning = log.warning.call_args[0]
    assert "no readable text" in warning[0]
    assert warning[1] == "length"
    assert warning[2] == ["reasoning"]


# ---------------------------------------------------------------------------
# System prompt: fast path, schema digest injection
# ---------------------------------------------------------------------------

_DIGEST = "- SalesTransaction [salesTransactions, salesTransactionsCount]"


def test_system_prompt_drops_deep_describe_advice():
    from agents.agent_dtwin_chat.engine import SYSTEM_PROMPT

    assert "depth ≥ 2" not in SYSTEM_PROMPT


def test_system_prompt_documents_filters_and_counts():
    from agents.agent_dtwin_chat.engine import SYSTEM_PROMPT

    assert "filters:[{field, value}]" in SYSTEM_PROMPT and "distinctBy" in SYSTEM_PROMPT


def test_build_system_prompt_injects_schema_digest():
    from agents.agent_dtwin_chat.engine import build_system_prompt

    assert _DIGEST in build_system_prompt(1, _DIGEST)


def test_build_system_prompt_without_digest_points_to_schema_tool():
    from agents.agent_dtwin_chat.engine import build_system_prompt

    prompt = build_system_prompt(1, "")
    assert "<<SCHEMA_DIGEST>>" not in prompt and "get_graphql_schema once" in prompt


def test_build_system_prompt_applies_describe_depth():
    from agents.agent_dtwin_chat.engine import build_system_prompt

    assert "``describe_entity`` (depth=3)" in build_system_prompt(3, _DIGEST)


def test_run_agent_sends_digest_in_system_message():
    seen = {}

    def llm_side_effect(_host, _token, _endpoint, messages, **_kwargs):
        seen["system"] = messages[0]["content"]
        return {"choices": [{"message": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}], "usage": {}}

    with patch(
        "agents.agent_dtwin_chat.engine.call_serving_endpoint",
        side_effect=llm_side_effect,
    ):
        run_agent(
            host="https://example.invalid",
            token="token",
            endpoint_name="endpoint",
            base_url="http://localhost:8000",
            domain_name="main",
            registry_params={},
            session_cookies={},
            user_message="who bought what",
            schema_digest=_DIGEST,
        )
    assert _DIGEST in seen["system"]


# ---------------------------------------------------------------------------
# Tool-history compaction (prompt-token bloat)
# ---------------------------------------------------------------------------


def _tool_turn(call_id: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "function": {"name": "query_graphql", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {},
    }


def _run_four_tool_iterations():
    import copy

    sent = []

    def llm_side_effect(_host, _token, _endpoint, messages, **_kwargs):
        sent.append(copy.deepcopy(messages))
        n = len(sent)
        if n <= 4:
            return _tool_turn(f"call{n}")
        return {"choices": [{"message": {"role": "assistant", "content": "done"},
                             "finish_reason": "stop"}], "usage": {}}

    def fake_dispatch(_handlers, _ctx, _tool_name, _arguments, *, trace_name=""):
        fake_dispatch.calls += 1
        return f"R{fake_dispatch.calls}:" + "x" * 5000

    fake_dispatch.calls = 0
    with (
        patch("agents.agent_dtwin_chat.engine.call_serving_endpoint",
              side_effect=llm_side_effect),
        patch("agents.agent_dtwin_chat.engine.dispatch_tool", side_effect=fake_dispatch),
    ):
        run_agent(
            host="https://example.invalid",
            token="token",
            endpoint_name="endpoint",
            base_url="http://localhost:8000",
            domain_name="main",
            registry_params={},
            session_cookies={},
            user_message="q",
        )
    return sent


def _tool_contents(messages):
    return [m["content"] for m in messages if m.get("role") == "tool"]


def test_older_tool_results_are_compacted_at_later_iterations():
    sent = _run_four_tool_iterations()
    contents = _tool_contents(sent[4])  # 5th call sees 4 tool results
    assert ["[compacted" in c for c in contents] == [True, True, False, False]


def test_compacted_tool_result_keeps_its_head():
    sent = _run_four_tool_iterations()
    first = _tool_contents(sent[4])[0]
    assert first.startswith("R1:") and len(first) < 2000


def test_compaction_keeps_tool_call_ids_paired():
    sent = _run_four_tool_iterations()
    messages = sent[4]
    call_ids = [
        tc["id"] for m in messages if m.get("role") == "assistant"
        for tc in m.get("tool_calls") or []
    ]
    result_ids = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    assert call_ids == result_ids == ["call1", "call2", "call3", "call4"]


def test_compaction_keeps_whole_latest_parallel_batch():
    from agents.agent_dtwin_chat.engine import compact_tool_messages

    big = "y" * 5000
    messages = [
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": big},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "b"}, {"id": "c"}, {"id": "d"}]},
        {"role": "tool", "tool_call_id": "b", "content": big},
        {"role": "tool", "tool_call_id": "c", "content": big},
        {"role": "tool", "tool_call_id": "d", "content": big},
    ]
    out = compact_tool_messages(messages, keep_full=2)
    assert [len(m["content"]) == 5000 for m in out if m["role"] == "tool"] == [
        False, True, True, True,
    ]


def test_compaction_does_not_mutate_the_input():
    from agents.agent_dtwin_chat.engine import compact_tool_messages

    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": "z" * 5000},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b"}]},
        {"role": "tool", "tool_call_id": "b", "content": "z" * 5000},
    ]
    compact_tool_messages(messages, keep_full=1)
    assert len(messages[1]["content"]) == 5000


def test_system_prompt_forbids_manual_unit_conversion():
    from agents.agent_dtwin_chat.engine import SYSTEM_PROMPT

    assert "never convert units yourself" in SYSTEM_PROMPT


def test_compaction_with_keep_full_zero_keeps_only_latest_batch():
    from agents.agent_dtwin_chat.engine import compact_tool_messages

    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": "z" * 5000},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b"}]},
        {"role": "tool", "tool_call_id": "b", "content": "z" * 5000},
    ]
    out = compact_tool_messages(messages, keep_full=0)
    assert [len(m["content"]) == 5000 for m in out if m["role"] == "tool"] == [False, True]


def test_compaction_skips_results_barely_longer_than_the_head():
    from agents.agent_dtwin_chat.engine import compact_tool_messages

    text = "z" * 1510
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": text},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b"}]},
    ]
    assert compact_tool_messages(messages, keep_full=0)[1]["content"] == text


# ---------------------------------------------------------------------------
# System prompt rendering
# ---------------------------------------------------------------------------


def _workflow_route_letters(prompt: str) -> list:
    import re

    workflow = prompt.split("WORKFLOW", 1)[1].split("\nRULES", 1)[0]
    return re.findall(r"^  ([A-Z])\. ", workflow, flags=re.MULTILINE)


def test_workflow_routes_are_lettered_in_order():
    from agents.agent_dtwin_chat.engine import SYSTEM_PROMPT

    letters = _workflow_route_letters(SYSTEM_PROMPT)
    assert letters == [chr(ord("A") + i) for i in range(len(letters))] and len(letters) == 10


def test_default_render_keeps_depth_one_wording():
    from agents.agent_dtwin_chat.engine import build_system_prompt

    prompt = build_system_prompt()
    assert "default ``describe_entity`` (depth=1). If you are not sure" in prompt


def test_render_leaves_no_placeholder_tokens():
    from agents.agent_dtwin_chat.engine import build_system_prompt

    assert "<<" not in build_system_prompt(2, _DIGEST)


def test_describe_depth_is_clamped_to_five():
    from agents.agent_dtwin_chat.engine import build_system_prompt

    assert "``describe_entity`` (depth=5)" in build_system_prompt(9, _DIGEST)


def test_prompt_mentions_sparql_only_when_a_sparql_tool_is_offered():
    from agents.agent_dtwin_chat.engine import build_system_prompt
    from agents.agent_dtwin_chat.tools import TOOL_DEFINITIONS

    offered = {d["function"]["name"] for d in TOOL_DEFINITIONS}
    assert ("SPARQL" in build_system_prompt()) == ("run_sparql" in offered)


def test_prompt_template_has_each_placeholder_once():
    from agents.agent_dtwin_chat import engine

    template = engine.SYSTEM_PROMPT
    assert (template.count(engine._SCHEMA_DIGEST_TOKEN), template.count(engine._DESCRIBE_DEPTH_TOKEN)) == (1, 1)


def test_prompt_links_only_uris_from_tool_responses():
    from agents.agent_dtwin_chat.engine import SYSTEM_PROMPT

    assert "Never build one from an id" in SYSTEM_PROMPT
    assert "ontobricks.com/ontology/Customer" not in SYSTEM_PROMPT


def test_drop_unresolvable_links_keeps_text_for_bare_ids():
    from agents.agent_dtwin_chat.engine import drop_unresolvable_links

    text = "[Head office](/resolve?uri=ORG_01) and [A](/resolve?uri=https://x/Org/A)"
    assert drop_unresolvable_links(text) == "Head office and [A](/resolve?uri=https://x/Org/A)"


def test_drop_unresolvable_links_leaves_other_links():
    from agents.agent_dtwin_chat.engine import drop_unresolvable_links

    text = "[docs](https://docs.databricks.com) [B](/resolve?uri=urn:x:1)"
    assert drop_unresolvable_links(text) == text


def test_prompt_covers_hierarchy_scope_and_same_name_items():
    from agents.agent_dtwin_chat.engine import SYSTEM_PROMPT

    assert "everything under" in SYSTEM_PROMPT and "roll-up fields (total*)" in SYSTEM_PROMPT
    assert "follow the parent link" in SYSTEM_PROMPT
    assert 'group by the label (by:["<relation>.label"])' in SYSTEM_PROMPT


def test_prompt_rules_are_domain_neutral():
    from agents.agent_dtwin_chat.engine import SYSTEM_PROMPT

    for customer_term in ("본부", "지사", "법인", "직속", "hasParentOrg", "totalRevenueKrw", "ofProduct"):
        assert customer_term not in SYSTEM_PROMPT


def test_prompt_requires_schema_check_before_claiming_absence():
    from agents.agent_dtwin_chat.engine import SYSTEM_PROMPT

    assert "Before saying a field, value or link does not exist" in SYSTEM_PROMPT
    assert "per-entity breakdown" in SYSTEM_PROMPT


def test_prompt_maps_state_words_to_categorical_filters():
    from agents.agent_dtwin_chat.engine import SYSTEM_PROMPT

    assert "names a state or kind" in SYSTEM_PROMPT
    assert "group by that field first" in SYSTEM_PROMPT
