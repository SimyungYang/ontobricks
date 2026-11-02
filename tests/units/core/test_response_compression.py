"""App responses are gzip-compressed for clients that accept it.

Explorer expansions return hundreds of KB of JSON; uncompressed, the
Korea → us-west-2 transfer dominated search latency (860 KB vs 28 KB gzipped).
Graph Chat's ``text/event-stream`` replies stay uncompressed so tokens stream.
"""

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from shared.fastapi.main import create_app

pytestmark = pytest.mark.unit


def _gzip_middleware(app: FastAPI):
    from starlette.middleware.gzip import GZipMiddleware

    return [m for m in app.user_middleware if m.cls is GZipMiddleware]


def test_app_registers_gzip_middleware():
    found = _gzip_middleware(create_app())
    assert len(found) == 1
    assert found[0].kwargs.get("minimum_size", 500) >= 500


def _probe_app() -> FastAPI:
    from starlette.middleware.gzip import GZipMiddleware

    (mw,) = _gzip_middleware(create_app())
    app = FastAPI()
    app.add_middleware(GZipMiddleware, **mw.kwargs)

    @app.get("/big")
    def big():
        return {"rows": ["https://example.org/Lot/LT_%04d" % i for i in range(500)]}

    @app.get("/stream")
    def stream():
        return StreamingResponse(iter(["data: a\n\n", "data: b\n\n"] * 200), media_type="text/event-stream")

    return app


def test_large_json_is_compressed():
    resp = TestClient(_probe_app()).get("/big", headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("content-encoding") == "gzip"


def test_event_streams_are_not_compressed():
    resp = TestClient(_probe_app()).get("/stream", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in resp.headers
