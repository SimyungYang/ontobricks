"""Unit tests for ``agents.tools.loopback_http`` header forwarding.

Databricks Apps sends ``X-Forwarded-Preferred-Username`` as the user's
display name in UTF-8. Starlette decodes header bytes as latin-1, so a
Korean name such as ``'시명 양'`` reaches the router as a str with
non-ASCII code points. httpx encodes str header values as ASCII, so the
agent's loopback client used to raise ``UnicodeEncodeError`` on every
Graph Chat tool call for those users.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from agents.agent_dtwin_chat import tools as chat_tools
from agents.tools import loopback_http
from agents.tools.context import ToolContext
from agents.tools.loopback_http import (
    loopback_client,
    loopback_forwarded_headers,
    loopback_wire_headers,
)

pytestmark = pytest.mark.unit

KOREAN_NAME = "시명 양"
KOREAN_NAME_UTF8 = KOREAN_NAME.encode("utf-8")
# What Starlette hands the router for the UTF-8 bytes on the wire.
KOREAN_NAME_AS_STARLETTE_STR = KOREAN_NAME_UTF8.decode("latin-1")

EMAIL = "simyung.yang@example.com"
USER_TOKEN = "dapi-user-token"


def _inbound_request(raw_headers: list[tuple[bytes, bytes]]) -> Request:
    """Build a Starlette request carrying the given raw wire headers."""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/dtwin/assistant/chat",
            "headers": raw_headers,
        }
    )


def _apps_proxy_headers() -> list[tuple[bytes, bytes]]:
    """Raw headers as the Databricks Apps proxy sends them for a Korean user."""
    return [
        (b"x-forwarded-email", EMAIL.encode()),
        (b"x-forwarded-user", b"1234567890@1111"),
        (b"x-forwarded-preferred-username", KOREAN_NAME_UTF8),
        (b"x-forwarded-access-token", USER_TOKEN.encode()),
        (b"x-real-ip", b"10.0.0.1"),
        (b"x-csrf-token", b"csrf-abc"),
        (b"referer", b"https://app.example.com/dtwin"),
        (b"cookie", b"session=abc"),
        (b"authorization", b"Bearer browser-token"),
        (b"content-type", b"application/json"),
    ]


def _install_mock_transport(monkeypatch, handler) -> None:
    """Route the real ``loopback_client`` through an httpx MockTransport."""
    real_client = httpx.Client

    def _client_with_transport(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(
        loopback_http, "httpx", SimpleNamespace(Client=_client_with_transport)
    )


class TestLoopbackForwardedHeaders:
    def test_keeps_identity_csrf_and_referer_headers(self):
        request = _inbound_request(_apps_proxy_headers())

        forwarded = loopback_forwarded_headers(request.headers)

        assert set(forwarded) == {
            "x-forwarded-email",
            "x-forwarded-user",
            "x-forwarded-preferred-username",
            "x-forwarded-access-token",
            "x-real-ip",
            "x-csrf-token",
            "referer",
        }

    def test_drops_cookie_authorization_and_body_headers(self):
        request = _inbound_request(_apps_proxy_headers())

        forwarded = loopback_forwarded_headers(request.headers)

        assert not {"cookie", "authorization", "content-type"} & set(forwarded)

    def test_matches_header_names_case_insensitively(self):
        forwarded = loopback_forwarded_headers(
            {"X-Forwarded-Email": EMAIL, "X-CSRF-Token": "t", "Host": "h"}
        )

        assert forwarded == {"X-Forwarded-Email": EMAIL, "X-CSRF-Token": "t"}


class TestLoopbackWireHeaders:
    def test_restores_original_utf8_bytes_from_starlette_str(self):
        wire = loopback_wire_headers(
            {"x-forwarded-preferred-username": KOREAN_NAME_AS_STARLETTE_STR}
        )

        assert wire["x-forwarded-preferred-username"] == KOREAN_NAME_UTF8

    def test_encodes_true_unicode_str_as_utf8(self):
        wire = loopback_wire_headers({"x-forwarded-preferred-username": KOREAN_NAME})

        assert wire["x-forwarded-preferred-username"] == KOREAN_NAME_UTF8

    def test_keeps_ascii_values_byte_identical(self):
        wire = loopback_wire_headers({"x-forwarded-email": EMAIL})

        assert wire["x-forwarded-email"] == EMAIL.encode("ascii")

    def test_passes_bytes_values_through(self):
        wire = loopback_wire_headers({"x-forwarded-email": b"raw"})

        assert wire["x-forwarded-email"] == b"raw"


class TestLoopbackClientNonAsciiDisplayName:
    def test_client_builds_with_starlette_decoded_korean_name(self):
        ctx = ToolContext(
            host="https://ws.example.com",
            token="tok",
            dtwin_base_url="http://loopback.invalid",
            dtwin_session_headers={
                "x-forwarded-preferred-username": KOREAN_NAME_AS_STARLETTE_STR
            },
        )

        with loopback_client(ctx) as client:
            raw = dict(client.headers.raw)

        assert raw[b"x-forwarded-preferred-username"] == KOREAN_NAME_UTF8

    def test_graph_chat_tool_call_succeeds_for_korean_display_name(self, monkeypatch):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200, json={"success": True, "has_data": True, "count": 3}
            )

        _install_mock_transport(monkeypatch, handler)
        inbound = _inbound_request(_apps_proxy_headers())
        ctx = ToolContext(
            host="https://ws.example.com",
            token="tok",
            dtwin_base_url="http://loopback.invalid",
            dtwin_session_headers=loopback_forwarded_headers(inbound.headers),
            dtwin_domain_name="sales_demo",
        )

        out = chat_tools.tool_get_status(ctx)

        assert "Status:  OK" in out
        assert seen[0].url.path == "/dtwin/sync/status"

    def test_loopback_receiver_resolves_same_identity_as_inbound(self, monkeypatch):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"success": True})

        _install_mock_transport(monkeypatch, handler)
        inbound = _inbound_request(_apps_proxy_headers())
        ctx = ToolContext(
            host="https://ws.example.com",
            token="tok",
            dtwin_base_url="http://loopback.invalid",
            dtwin_session_headers=loopback_forwarded_headers(inbound.headers),
        )

        chat_tools.tool_get_status(ctx)

        # Re-parse the loopback request the way the receiving FastAPI app does.
        received = _inbound_request(list(seen[0].headers.raw)).headers
        identity = ("x-forwarded-email", "x-forwarded-user", "x-forwarded-access-token",
                    "x-forwarded-preferred-username", "x-csrf-token")
        assert {k: received.get(k) for k in identity} == {
            k: inbound.headers.get(k) for k in identity
        }
