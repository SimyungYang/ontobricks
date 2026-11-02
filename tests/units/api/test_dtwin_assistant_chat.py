"""Response payload tests for Graph Chat assistant routes."""

from types import SimpleNamespace
from unittest.mock import patch

from agents.agent_dtwin_chat.engine import SYSTEM_PROMPT, AgentResult
from api.routers.internal import dtwin

_PENDING_ACTION = {
    "token": "confirm-token",
    "entity_uri": "https://example.com/Customer/CUST1",
    "action": "main.ops.recompute_risk",
}


def test_system_prompt_documents_dataset_bridge_and_action_workflow():
    assert "CONTEXT (ontology design)" in SYSTEM_PROMPT
    assert "get_entity_context(entity_uri, fetch_dataset_rows?, follow_bridges?)" in SYSTEM_PROMPT
    assert "request_entity_action(entity_uri, action)" in SYSTEM_PROMPT
    assert "Never claim an Action ran" in SYSTEM_PROMPT


def test_chat_payload_includes_pending_action_for_json_and_sse_done():
    result = AgentResult(
        success=True,
        reply="Action is ready for confirmation.",
        pending_action=_PENDING_ACTION,
    )

    blocking_payload = dtwin._chat_response_payload(result)
    stream_payload = dtwin._chat_response_payload(result, event_type="done")

    assert blocking_payload["pending_action"] == _PENDING_ACTION
    assert "type" not in blocking_payload
    assert stream_payload["type"] == "done"
    assert stream_payload["pending_action"] == _PENDING_ACTION


def test_chat_rejects_empty_domain_llm_before_running_agent(client):
    domain = SimpleNamespace(info={})

    with (
        patch.object(dtwin, "get_domain", return_value=domain),
        patch("agents.agent_dtwin_chat.run_agent") as run_chat_agent,
    ):
        response = client.post("/dtwin/assistant/chat", json={"message": "Hello"})

    assert response.status_code == 400
    assert response.json()["message"] == (
        "No LLM selected. Select one in Domain → General → AI."
    )
    run_chat_agent.assert_not_called()


def test_chat_tool_calls_succeed_for_non_ascii_display_name(client, monkeypatch):
    """A Korean display name must not break the agent's loopback tool calls.

    The Apps proxy sends ``X-Forwarded-Preferred-Username`` as UTF-8 bytes;
    the route forwards it onto the agent's httpx loopback client, which
    used to fail with ``'ascii' codec can't encode characters``.
    """
    import httpx

    from agents.agent_dtwin_chat import tools as chat_tools
    from agents.tools import loopback_http
    from agents.tools.context import ToolContext

    korean_name = "시명 양".encode("utf-8")
    loopback_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        loopback_requests.append(request)
        return httpx.Response(200, json={"success": True, "has_data": True, "count": 1})

    real_client = httpx.Client
    monkeypatch.setattr(
        loopback_http,
        "httpx",
        SimpleNamespace(
            Client=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
        ),
    )

    def fake_run_agent(**kwargs):
        ctx = ToolContext(
            host=kwargs["host"],
            token=kwargs["token"],
            dtwin_base_url=kwargs["base_url"],
            dtwin_session_cookies=kwargs["session_cookies"],
            dtwin_session_headers=kwargs["session_headers"],
            dtwin_domain_name=kwargs["domain_name"],
        )
        return AgentResult(success=True, reply=chat_tools.tool_get_status(ctx))

    domain = SimpleNamespace(info={})
    with (
        patch.object(dtwin, "get_domain", return_value=domain),
        patch.object(dtwin, "require_domain_llm", return_value=("h", "t", "ep", "fm")),
        patch.object(dtwin.DigitalTwin, "resolve_registry", return_value={}),
        patch.object(dtwin, "_chat_resolve_domain_name", return_value="sales_demo"),
        patch.object(dtwin, "_chat_schema_digest", return_value=""),
        patch.object(dtwin, "_chat_save_cache"),
        patch("agents.agent_dtwin_chat.run_agent", side_effect=fake_run_agent),
    ):
        response = client.post(
            "/dtwin/assistant/chat",
            json={"message": "status?"},
            headers={
                "x-forwarded-email": "simyung.yang@example.com",
                "x-forwarded-access-token": "dapi-user-token",
                "x-forwarded-preferred-username": korean_name,
            },
        )

    assert response.status_code == 200
    assert "Status:  OK" in response.json()["reply"]
    sent = dict(loopback_requests[0].headers.raw)
    assert sent[b"x-forwarded-preferred-username"] == korean_name
    assert sent[b"x-forwarded-email"] == b"simyung.yang@example.com"
    assert sent[b"x-forwarded-access-token"] == b"dapi-user-token"
