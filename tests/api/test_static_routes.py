"""
tests/api/test_static_routes.py
-------------------------------
Regression guard for the crawler + legal-page 404s seen in production logs
(/robots.txt, /sitemap.xml, /terms-of-service, /privacy-policy, /disclaimer).

On Vercel these resolve straight from the filesystem, but uvicorn (local dev +
Render) 404s without explicit routes — the footer's clean-URL links and
crawlers hit a JSON 404. These routes serve the files directly so both hosts
behave the same. Must NOT shadow real APIs.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app


def test_robots_txt_served() -> None:
    r = TestClient(app).get("/robots.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    # It's the real robots file (references the sitemap), not the SPA shell.
    assert "Sitemap:" in r.text
    assert "<!doctype html" not in r.text.lower()


def test_sitemap_xml_served() -> None:
    r = TestClient(app).get("/sitemap.xml")
    assert r.status_code == 200
    assert "xml" in r.headers["content-type"]
    assert "<!doctype html" not in r.text.lower()


def test_legal_pages_served() -> None:
    client = TestClient(app)
    for path in ("/terms-of-service", "/privacy-policy", "/disclaimer"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert r.headers["content-type"].startswith("text/html"), path
        # The right standalone page, not the SPA shell fallback.
        assert "<html" in r.text.lower(), path


def test_legal_route_is_exact_not_catch_all() -> None:
    """The legal routes must be exact paths — a nested path must still 404, so
    they don't accidentally become an SPA-style catch-all."""
    r = TestClient(app).get("/terms-of-service/extra")
    assert r.status_code == 404
