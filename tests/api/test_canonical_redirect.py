"""
tests/api/test_canonical_redirect.py
------------------------------------
Regression guard for the auctionscope.in domain cutover.

This service also answers on api.auctionscope.in and *.onrender.com. Serving the
SPA page shell there creates a second/third origin, and because the Supabase
session lives in per-origin localStorage, a user who logs in on one origin looks
logged-out on the canonical site (the bug behind "after login I still see the old
URL / refresh logs me out"). So browser page loads on the API hosts must 301 to
the canonical web origin, while API routes keep answering on every host.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app

CANONICAL = "https://www.auctionscope.in"
client = TestClient(app)

# Every route that hands back the SPA shell (index.html) or an admin page.
PAGE_ROUTES = ["/", "/chat", "/chat/thread-abc123", "/property/abc123", "/admin", "/review"]
# Hosts this same service answers on that are NOT the canonical frontend.
API_HOSTS = ["api.auctionscope.in", "auction-api-w68b.onrender.com", "staging.onrender.com"]
# The canonical frontend host plus local-dev hosts that must keep serving.
SERVING_HOSTS = ["www.auctionscope.in", "localhost", "127.0.0.1", "testserver"]


@pytest.mark.parametrize("path", PAGE_ROUTES)
@pytest.mark.parametrize("host", API_HOSTS)
def test_spa_page_on_api_host_redirects_to_canonical(path, host):
    resp = client.get(path, headers={"host": host}, follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == f"{CANONICAL}{path}"


def test_redirect_preserves_path_and_query():
    resp = client.get(
        "/property/123?ref=email",
        headers={"host": "api.auctionscope.in"},
        follow_redirects=False,
    )
    assert resp.status_code == 301
    assert resp.headers["location"] == f"{CANONICAL}/property/123?ref=email"


@pytest.mark.parametrize("path", PAGE_ROUTES)
@pytest.mark.parametrize("host", SERVING_HOSTS)
def test_spa_page_served_on_canonical_and_local_hosts(path, host):
    resp = client.get(path, headers={"host": host}, follow_redirects=False)
    assert resp.status_code == 200
    assert "location" not in resp.headers


@pytest.mark.parametrize("host", API_HOSTS + SERVING_HOSTS)
def test_api_route_never_redirected(host):
    # /health is the Render health check and a representative API route. It must
    # answer on every host, including the API hosts that redirect page loads —
    # otherwise the canonical redirect would break the API and the deploy.
    resp = client.get("/health", headers={"host": host}, follow_redirects=False)
    assert resp.status_code == 200
    assert "location" not in resp.headers
