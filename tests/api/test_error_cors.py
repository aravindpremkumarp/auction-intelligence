"""The catch-all 500 handler must echo Access-Control-Allow-Origin.

Starlette emits unhandled-exception 500s from ServerErrorMiddleware, which
sits outside CORSMiddleware — so without an explicit header the browser
discards the response and the fetch rejects as "Failed to fetch", hiding the
real status. (This is what made the property-detail 500 look like a network
failure in the UI.)
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


def test_unhandled_500_carries_cors_header(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the detail endpoint to raise so the catch-all handler runs.
    # The route calls get_auction_detail(), so patch that symbol on the router
    # module (it's imported there from api.tools.cypher_tools).
    mod = importlib.import_module("api.properties.router")

    def boom(auction_id: str):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(mod, "get_auction_detail", boom)

    from api.main import app
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/auction/anything", headers={"Origin": "https://auctionscope.in"})

    assert r.status_code == 500
    assert r.json() == {"detail": "internal server error"}
    # Without this header the browser reports "Failed to fetch" instead of 500.
    assert r.headers.get("access-control-allow-origin") == "https://auctionscope.in"


def test_origin_allowed_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.main import _origin_allowed

    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.delenv("APP_BASE_URL", raising=False)

    assert _origin_allowed("https://auctionscope.in")
    assert _origin_allowed("https://www.auctionscope.in")
    assert _origin_allowed("https://preview-abc.vercel.app")
    assert not _origin_allowed("https://evil.example.com")
    assert not _origin_allowed("http://auctionscope.in")   # http is not allowed
    assert not _origin_allowed("https://auctionscope.in.evil.com")
    assert not _origin_allowed(None)
    assert not _origin_allowed("")
