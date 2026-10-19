"""
MLflow tracing configuration for OntoBricks agents.

Provides helpers to initialise MLflow experiment tracking and tracing,
plus a safe decorator that degrades to a no-op when MLflow is unavailable
or not configured.
"""

import json
import os
import functools
import inspect
from typing import Any, Optional

from back.core.logging import get_logger

logger = get_logger(__name__)

_TRACING_READY = False

EXPERIMENT_NAME = os.getenv("ONTOBRICKS_MLFLOW_EXPERIMENT", "ontobricks-agents")

# Per-field character cap for message / tool payloads recorded on spans.
TRACE_MAX_CHARS = int(os.getenv("ONTOBRICKS_TRACE_MAX_CHARS", "4000"))
# Argument names never recorded on a span: credentials (token, host,
# client) and opaque runtime objects (ctx, handlers) with no trace value.
_UNTRACED_ARG_NAMES = frozenset({"token", "host", "client", "ctx", "handlers"})


def trace_payloads_enabled() -> bool:
    """Return True when message / tool payloads should be recorded on spans.

    On by default so a failed chat turn can be diagnosed from the trace alone;
    set ``ONTOBRICKS_TRACE_PAYLOADS=false`` to record only sizes and counts.
    """
    return os.getenv("ONTOBRICKS_TRACE_PAYLOADS", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _resolve_experiment_name(name: str) -> str:
    """Ensure the experiment name is an absolute workspace path on Databricks.

    Databricks MLflow requires paths like ``/Users/<user>/...`` or ``/Shared/...``.
    When running locally (no tracking URI or file-based), the name is returned as-is.
    """
    if name.startswith("/"):
        return name

    tracking_uri = os.getenv("MLFLOW_TRACKING_URI", "")
    if tracking_uri.lower() != "databricks":
        return name

    return f"/Shared/{name}"


def setup_tracing(experiment_name: Optional[str] = None) -> bool:
    """Initialise MLflow tracing for agent runs.

    Call once during application startup.  Returns *True* if tracing was
    successfully configured, *False* otherwise (the application can still
    run without tracing).
    """
    global _TRACING_READY
    try:
        import mlflow

        name = _resolve_experiment_name(experiment_name or EXPERIMENT_NAME)
        mlflow.set_experiment(name)
        mlflow.tracing.enable()
        _TRACING_READY = True
        logger.info("MLflow tracing enabled — experiment='%s'", name)
        return True
    except Exception as exc:
        logger.warning(
            "MLflow tracing setup failed (agents will run without tracing): %s", exc
        )
        _TRACING_READY = False
        return False


def is_tracing_ready() -> bool:
    return _TRACING_READY


def trace_agent(name: Optional[str] = None, *, stage: Optional[str] = None):
    """Decorator: wrap an agent entry point with an AGENT span.

    ``stage`` and the caller-supplied ``draft_id`` / ``draft_revision`` kwargs
    are attached to the span as **span attributes** (``Span.set_attributes``)
    so a staged, resumable run's traces correlate across the checkpointed
    substages and an eval harness can assert stage identity directly from the
    trace (SPEC §8). These are span-level attributes, not MLflow trace-level
    tags (``mlflow.set_trace_tag`` / trace ``tags=``) — this module does not
    use that API.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not _TRACING_READY:
                return fn(*args, **kwargs)
            import mlflow
            from mlflow.entities import SpanType

            with mlflow.start_span(
                name=name or fn.__name__, span_type=SpanType.AGENT
            ) as span:
                span.set_inputs(_safe_inputs(kwargs))
                attributes = {}
                if stage:
                    attributes["stage"] = stage
                for key in ("draft_id", "draft_revision"):
                    value = kwargs.get(key)
                    if value is not None:
                        attributes[key] = str(value)
                if attributes:
                    span.set_attributes(attributes)
                result = fn(*args, **kwargs)
                span.set_outputs(_safe_result(result))
                return result

        return wrapper

    return decorator


def trace_llm(name: Optional[str] = None):
    """Decorator: wrap an LLM call function with an LLM span."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not _TRACING_READY:
                return fn(*args, **kwargs)
            import mlflow
            from mlflow.entities import SpanType

            with mlflow.start_span(
                name=name or fn.__name__, span_type=SpanType.LLM
            ) as span:
                bound = _bind_call_arguments(fn, args, kwargs)
                messages = bound.get("messages") or []
                tools = bound.get("tools") or []
                inputs = {
                    "endpoint": bound.get("endpoint_name") or "?",
                    "message_count": len(messages),
                    "tool_count": len(tools),
                    "max_tokens": bound.get("max_tokens"),
                }
                if trace_payloads_enabled():
                    inputs["messages"] = _summarize_messages(messages)
                span.set_inputs(inputs)
                result = fn(*args, **kwargs)
                if isinstance(result, dict):
                    span.set_outputs(_summarize_llm_response(result))
                return result

        return wrapper

    return decorator


def trace_tool(name: Optional[str] = None):
    """Decorator: wrap a tool-dispatch function with a TOOL span.

    The tool's name is a span *input* (``tool_name``); the span *output*
    carries ``result_length`` and a ``status`` of ``"ok"``/``"error"`` derived
    from whether the JSON result string carries a top-level ``error`` key —
    so a tool call's name and outcome are both visible on the trace without
    changing what the wrapped function returns.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not _TRACING_READY:
                return fn(*args, **kwargs)
            import mlflow
            from mlflow.entities import SpanType

            tool_name, tool_args = _bind_tool_call(fn, name, args, kwargs)
            with mlflow.start_span(
                name=f"tool:{tool_name}", span_type=SpanType.TOOL
            ) as span:
                span.set_inputs(
                    {
                        "tool_name": tool_name,
                        "arguments": _truncate(str(tool_args), TRACE_MAX_CHARS),
                    }
                )
                result = fn(*args, **kwargs)
                outputs: dict = {
                    "result_length": len(result) if isinstance(result, str) else None,
                    "status": _tool_result_status(result),
                }
                if trace_payloads_enabled() and isinstance(result, str):
                    outputs["result"] = _truncate(result, TRACE_MAX_CHARS)
                span.set_outputs(outputs)
                return result

        return wrapper

    return decorator


def _tool_result_status(result: Any) -> str:
    """``"error"`` when *result* is a JSON string carrying a top-level
    ``error`` key (the shared ``dispatch_tool``/tool-handler error shape);
    ``"ok"`` otherwise, including non-JSON or non-string results."""
    if not isinstance(result, str):
        return "ok"
    try:
        parsed = json.loads(result)
    except (ValueError, TypeError):
        return "ok"
    return "error" if isinstance(parsed, dict) and "error" in parsed else "ok"


def _safe_inputs(kwargs: dict) -> dict:
    """Extract trace-safe inputs from kwargs, excluding untraced names."""
    out = {}
    for k, v in kwargs.items():
        if k in _UNTRACED_ARG_NAMES:
            continue
        if isinstance(v, (str, int, float, bool, type(None))):
            out[k] = _truncate(str(v), 300)
        elif isinstance(v, list):
            out[k] = f"[list, len={len(v)}]"
        elif isinstance(v, dict):
            out[k] = f"[dict, keys={list(v.keys())[:10]}]"
        else:
            out[k] = type(v).__name__
    return out


def _safe_result(result) -> dict:
    """Serialise an AgentResult-like object for the span output."""
    if result is None:
        return {}
    out = {}
    for attr in ("success", "iterations", "error", "usage"):
        val = getattr(result, attr, None)
        if val is not None:
            out[attr] = val
    reply = getattr(result, "reply", None)
    if trace_payloads_enabled() and isinstance(reply, str) and reply:
        out["reply"] = _truncate(reply, TRACE_MAX_CHARS)
    return out


def _bind_call_arguments(fn, args: tuple, kwargs: dict) -> dict:
    """Map a call's positional and keyword arguments to *fn*'s parameter names.

    Only explicitly passed arguments are returned (defaults are not filled
    in). Falls back to *kwargs* when the call does not match the signature.
    """
    try:
        return dict(inspect.signature(fn).bind_partial(*args, **kwargs).arguments)
    except TypeError:
        return dict(kwargs)


def _bind_tool_call(fn, default_name: Optional[str], args: tuple, kwargs: dict):
    """Return ``(tool_name, arguments)`` for a traced tool function call.

    Works for both dispatchers (``dispatch_tool(handlers, ctx, tool_name,
    arguments)``) and individual tools (``tool_x(ctx, *, uri=...)``), where the
    tool name falls back to the decorator name and the arguments are the bound
    call parameters minus context objects and secrets.
    """
    bound = _bind_call_arguments(fn, args, kwargs)
    tool_name = bound.get("tool_name") or default_name or fn.__name__
    if "arguments" in bound:
        tool_args = bound["arguments"]
    else:
        tool_args = {}
        for key, val in bound.items():
            if key in _UNTRACED_ARG_NAMES:
                continue
            if key == "_kwargs" or key == "kwargs":
                tool_args.update(val or {})
            else:
                tool_args[key] = val
    return tool_name, tool_args


def _content_text(content: Any, markers: bool = True) -> str:
    """Flatten an OpenAI / Anthropic-style message content into readable text.

    Text blocks keep their text; other blocks (reasoning, images, …) are shown
    as ``[<type>]`` markers (or dropped when *markers* is False) so their
    presence is visible without leaking opaque payloads such as reasoning
    signatures.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            filter(None, (_content_text(item, markers) for item in content))
        )
    if isinstance(content, dict):
        block_type = content.get("type")
        if block_type in (None, "text", "output_text"):
            for key in ("text", "content", "value"):
                if key in content:
                    return _content_text(content[key], markers)
        return f"[{block_type}]" if markers else ""
    return str(content)


def content_block_types(content: Any) -> list:
    """Return the block types of a message content (``["str"]`` for plain text)."""
    if isinstance(content, list):
        return [
            (b.get("type") if isinstance(b, dict) else type(b).__name__)
            for b in content
        ]
    if content is None:
        return []
    return [type(content).__name__]


def _summarize_tool_calls(tool_calls: Any) -> list:
    out = []
    for call in tool_calls or []:
        func = (call or {}).get("function", {}) if isinstance(call, dict) else {}
        out.append(
            {
                "name": func.get("name"),
                "arguments": _truncate(str(func.get("arguments", "")), TRACE_MAX_CHARS),
            }
        )
    return out


def _summarize_messages(messages: list) -> list:
    """Return a trace-safe copy of chat *messages* with truncated contents."""
    out = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        entry: dict = {
            "role": msg.get("role"),
            "content": _truncate(_content_text(msg.get("content")), TRACE_MAX_CHARS),
        }
        if msg.get("tool_calls"):
            entry["tool_calls"] = _summarize_tool_calls(msg["tool_calls"])
        if msg.get("tool_call_id"):
            entry["tool_call_id"] = msg["tool_call_id"]
        out.append(entry)
    return out


def _summarize_llm_response(result: dict) -> dict:
    """Return finish reason, token usage and (optionally) the reply for a span."""
    choice = (result.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    usage = result.get("usage") or {}
    content = message.get("content")
    text = _content_text(content)
    out = {
        "finish_reason": choice.get("finish_reason"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "content_block_types": content_block_types(content),
        "text_length": len(_content_text(content, markers=False)),
        "tool_calls": _summarize_tool_calls(message.get("tool_calls")),
    }
    if trace_payloads_enabled():
        out["text"] = _truncate(text, TRACE_MAX_CHARS)
    return out


def _truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit] + "…"
