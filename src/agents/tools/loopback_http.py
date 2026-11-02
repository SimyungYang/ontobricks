"""
Shared loopback HTTP helpers for agent tool modules.

Both ``agent_dtwin_chat/tools.py`` and ``agent_cohort/tools.py`` issue
synchronous HTTP requests over loopback to the running OntoBricks app.
This module centralises the common plumbing so each agent only has to
supply its name and, optionally, a timeout.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from agents.tools.context import ToolContext
from shared.config.constants import HTTP_USER_AGENT

_DEFAULT_TIMEOUT = 60

# Databricks-Apps identity + CSRF headers forwarded on the agents' loopback
# calls so they pass PermissionMiddleware (which otherwise 302-redirects the
# anonymous internal request to ``/access-denied``).
_FORWARDED_HEADER_PREFIXES = ("x-forwarded-", "x-real-")
_FORWARDED_EXTRA_HEADERS = frozenset({"x-csrf-token", "referer"})


def loopback_forwarded_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Select the inbound request headers an agent forwards on loopback calls."""
    return {
        k: v
        for k, v in headers.items()
        if k.lower().startswith(_FORWARDED_HEADER_PREFIXES)
        or k.lower() in _FORWARDED_EXTRA_HEADERS
    }


def loopback_wire_headers(headers: Mapping[str, str | bytes]) -> dict[str, bytes]:
    """Encode header values as the raw bytes they arrived with on the wire.

    Starlette decodes header bytes as latin-1, so a UTF-8 display name
    (``X-Forwarded-Preferred-Username``) becomes a str with non-ASCII code
    points, which httpx refuses to encode as ASCII. Re-encoding as latin-1
    restores the exact inbound bytes; a str holding real Unicode (code points
    above U+00FF, never produced by Starlette) is sent as UTF-8.
    """
    return {k: _wire_header_value(v) for k, v in headers.items()}


def _wire_header_value(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        return value
    try:
        return value.encode("latin-1")
    except UnicodeEncodeError:
        return value.encode("utf-8")


def loopback_client(ctx: ToolContext, *, timeout: int = _DEFAULT_TIMEOUT) -> httpx.Client:
    """Build a sync httpx.Client bound to the loopback OntoBricks URL.

    Session cookies AND the user's Databricks-Apps ``X-Forwarded-*``
    identity headers are forwarded so the loopback route resolves the
    same active session *and* passes the ``PermissionMiddleware`` on
    the deployed app. Header values go out as raw bytes so a non-ASCII
    display name cannot break the call.
    """
    headers = {"User-Agent": HTTP_USER_AGENT, **(ctx.dtwin_session_headers or {})}
    return httpx.Client(
        base_url=ctx.dtwin_base_url or "http://localhost:8000",
        cookies=ctx.dtwin_session_cookies or {},
        headers=loopback_wire_headers(headers),
        timeout=timeout,
        follow_redirects=False,
    )


def loopback_registry_params(ctx: ToolContext) -> dict:
    """Build the registry query-parameter dict from the tool context."""
    return {k: v for k, v in (ctx.dtwin_registry_params or {}).items() if v}
