"""Smoke test: the API module must import cleanly."""
from __future__ import annotations


def test_app_imports() -> None:
    from api.main import app
    routes = {r.path for r in app.routes}
    assert "/feedback" in routes
    assert "/feedback/recent" in routes
    assert "/feedback/{feedback_id}/resolve" in routes
