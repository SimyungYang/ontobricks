"""
Shared infrastructure for OntoBricks agent engines.

Provides the common ``AgentStep`` dataclass and reusable helpers for LLM
serving-endpoint calls, tool dispatch, response content extraction, and
token usage accumulation.  Each concrete agent engine imports what it needs
and focuses exclusively on its own ``AgentResult``, system prompt, and
``run_agent`` loop.
"""

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import requests

from back.core.logging import get_logger
from agents.llm_utils import call_llm_with_retry
from agents.tracing import trace_llm
from shared.llm_target import build_llm_request

logger = get_logger(__name__)

# Endpoints (e.g. databricks-claude-opus-4-7) sometimes reject optional
# OpenAI-style parameters with a 400 message like:
#   "Model ... does not support the temperature parameter."
# We cache such bans per (endpoint, param) pair so subsequent calls skip the
# offending field proactively instead of re-discovering the 400 every time.
_UNSUPPORTED_PARAMS: Dict[str, set] = {}


def _unsupported_params(endpoint_name: str) -> set:
    return _UNSUPPORTED_PARAMS.setdefault(endpoint_name, set())


# Newer Anthropic models behind the AI Gateway reject json_schema output with
# "Structured output is not supported for this model ..." — no mention of the
# parameter name, so it needs its own phrase.
_PARAM_REJECTION_PHRASES = {
    "response_format": ("structured output is not supported",),
}


# Optional request fields a model may reject; stripped and banned per endpoint.
_STRIPPABLE_PARAMS = ("temperature", "response_format")


def _looks_unsupported(body_text: str, param: str) -> bool:
    low = (body_text or "").lower()
    return (
        f"does not support the {param} parameter" in low
        or ("unsupported" in low and param in low)
        or any(phrase in low for phrase in _PARAM_REJECTION_PHRASES.get(param, ()))
    )


# =====================================================
# Shared data class
# =====================================================


@dataclass
class AgentStep:
    """One observable step of the agent's execution."""

    step_type: str  # tool_call | tool_result | output
    content: str
    tool_name: str = ""
    duration_ms: int = 0


# =====================================================
# LLM call helper
# =====================================================


@trace_llm("agent:llm")
def call_serving_endpoint(
    host: str,
    token: str,
    endpoint_name: str,
    messages: List[dict],
    *,
    tools: Optional[List[dict]] = None,
    response_format: Optional[dict] = None,
    max_tokens: int = 2048,
    temperature: float = 0.1,
    timeout: int = 180,
    trace_name: str = "agent:llm",
    endpoint_kind: str = "",
) -> dict:
    """Call a Databricks serving endpoint (OpenAI-compatible chat completions).

    Builds the URL, headers, and payload, then delegates to
    :func:`call_llm_with_retry` for retry/backoff logic.

    ``response_format`` is an opt-in OpenAI/Databricks-style structured-
    output directive (e.g. ``{"type": "json_schema", "json_schema": {...}}``)
    for transport-level schema enforcement — see
    :func:`shared.llm_target.build_llm_request` for the ``tools`` +
    ``response_format`` mutual-exclusion guard. When an endpoint rejects
    ``response_format`` with a clear 400 (the same "does not support the
    ... parameter" shape already handled for ``temperature``), it is
    stripped and the request retried once, then the ban is cached per
    endpoint so later calls skip it proactively.

    Args:
        trace_name: Used for MLflow span naming via ``@trace_llm``.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    banned = _unsupported_params(endpoint_name)
    url, payload = build_llm_request(
        host,
        endpoint_name,
        endpoint_kind,
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        tools=tools,
        response_format=response_format,
    )
    for param in banned:
        payload.pop(param, None)

    logger.info(
        "%s: POST %s — %d messages, %d tool defs, response_format=%s, "
        "max_tokens=%d, temperature=%s",
        trace_name,
        endpoint_name,
        len(messages),
        len(tools) if tools else 0,
        bool(payload.get("response_format")),
        max_tokens,
        payload.get("temperature", "<skipped>"),
    )

    # Endpoints name one rejected param per 400 (e.g. response_format, then
    # temperature on the retry), so strip and retry until none is left.
    for _ in range(len(_STRIPPABLE_PARAMS) + 1):
        try:
            return call_llm_with_retry(url, headers, payload, timeout=timeout).json()
        except requests.exceptions.HTTPError as exc:
            dropped = _strip_rejected_params(exc, payload, banned)
            if not dropped:
                raise
            logger.warning(
                "%s: endpoint rejected unsupported param(s) %s — retrying without them",
                trace_name,
                dropped,
            )
    raise RuntimeError("call_serving_endpoint: unexpected exit")


def _strip_rejected_params(
    exc: requests.exceptions.HTTPError, payload: Dict[str, Any], banned: set
) -> List[str]:
    """Remove the params a 400 says the model rejects; return what was removed."""
    response = exc.response
    if response is None or response.status_code != 400:
        return []
    dropped = [
        param
        for param in _STRIPPABLE_PARAMS
        if param in payload and _looks_unsupported(response.text, param)
    ]
    for param in dropped:
        banned.add(param)
        payload.pop(param, None)
    return dropped


# =====================================================
# Tool dispatch helper
# =====================================================


def dispatch_tool(
    handlers: Dict[str, Callable],
    ctx: Any,
    tool_name: str,
    arguments: dict,
    *,
    trace_name: str = "agent:tool",
) -> str:
    """Dispatch a tool call and return the JSON result string.

    Handles unknown tools and exceptions uniformly across agents.
    """
    handler = handlers.get(tool_name)
    if not handler:
        logger.warning(
            "%s: unknown tool '%s' — available: %s",
            trace_name,
            tool_name,
            list(handlers.keys()),
        )
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
    try:
        t0 = time.time()
        result = handler(ctx, **arguments)
        elapsed = int((time.time() - t0) * 1000)
        logger.info(
            "%s: '%s' completed in %dms, returned %d chars",
            trace_name,
            tool_name,
            elapsed,
            len(result),
        )
        return result
    except Exception as exc:
        logger.exception("%s: '%s' raised exception: %s", trace_name, tool_name, exc)
        return json.dumps({"error": f"Tool execution failed: {exc}"})


# =====================================================
# Response content extraction
# =====================================================


def extract_message_content(llm_response: dict) -> str:
    """Extract text content from an OpenAI-style or predictions-style LLM response."""
    choices = llm_response.get("choices", [])
    if choices:
        content = choices[0].get("message", {}).get("content") or ""
        # Claude endpoints return content as a list of blocks, not a string
        if isinstance(content, list):
            content = "".join(
                b if isinstance(b, str) else b.get("text", "") for b in content
            )
        return content
    preds = llm_response.get("predictions", [])
    if preds:
        return preds[0] if isinstance(preds[0], str) else str(preds[0])
    logger.warning(
        "extract_message_content: no choices or predictions, keys=%s",
        list(llm_response.keys()),
    )
    return ""


# =====================================================
# Token usage accumulation
# =====================================================


def accumulate_usage(total: Dict[str, int], usage_block: dict) -> None:
    """Add prompt/completion token counts from *usage_block* into *total* in-place."""
    total["prompt_tokens"] = total.get("prompt_tokens", 0) + usage_block.get(
        "prompt_tokens", 0
    )
    total["completion_tokens"] = total.get("completion_tokens", 0) + usage_block.get(
        "completion_tokens", 0
    )
