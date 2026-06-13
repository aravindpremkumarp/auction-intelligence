"""SPA deep-link fallback routes.

Regression: ISSUE-002 — local SPA deep links die on JSON errors
Found by /qa on 2026-06-11
Report: .gstack/qa-reports/qa-report-localhost-8000-2026-06-11.md

The client router (web/index.html) pushes `/chat` and `/property/{id}`. On a
fresh load / refresh the browser GETs those paths; before the fix the dev
server returned 405 (/chat had only POST) and 404 (/property/{id} had no
route), so refreshing any non-root screen died on a raw JSON error instead of
booting the app. These routes must serve index.html so the SPA boots and its
own router renders the screen — WITHOUT shadowing the real APIs (POST /chat,
GET /watchlist).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app


def _index_html() -> str:
    from api.main import WEB_DIR
    return (WEB_DIR / "index.html").read_text(encoding="utf-8", errors="ignore")


def test_get_chat_serves_spa_shell() -> None:
    client = TestClient(app)
    r = client.get("/chat")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # It's the app shell, not some other page.
    assert "<title>" in r.text.lower() or "<!doctype html" in r.text.lower()


def test_get_property_deep_link_serves_spa_shell() -> None:
    client = TestClient(app)
    r = client.get("/property/747277")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_property_route_matches_single_segment_only() -> None:
    """The {auction_id} param must not over-capture nested paths into the SPA
    shell — a deeper path has no SPA route and should still 404."""
    client = TestClient(app)
    r = client.get("/property/747277/extra")
    assert r.status_code == 404


def test_post_chat_still_routed_to_api_not_shell() -> None:
    """Adding GET /chat must not shadow the chat API (POST /chat). An empty
    body fails request validation (422) — proving it reached the API handler,
    not the HTML fallback."""
    client = TestClient(app)
    r = client.post("/chat", json={})
    assert r.status_code == 422
    assert "text/html" not in r.headers.get("content-type", "")


def test_get_watchlist_still_data_api_not_shell() -> None:
    """GET /watchlist is the authenticated data API; the SPA fallback must not
    shadow it. Unauthenticated → 401, and never HTML."""
    client = TestClient(app)
    r = client.get("/watchlist")
    assert r.status_code == 401
    assert "text/html" not in r.headers.get("content-type", "")
