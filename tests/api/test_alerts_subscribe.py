"""Endpoint tests for POST /alerts/subscribe (anonymous email capture).

The repository write is faked so these pin the router contract: valid signups
are normalized + forwarded to the repo and return 200; bad emails are rejected
by validation (422) and never reach the repo.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Record what the router hands the repository, without touching Neo4j."""
    from api.alerts import repository as repo

    calls: list[dict] = []

    async def upsert_subscriber(email, city, property_type, source, created_at):
        calls.append({"email": email, "city": city, "property_type": property_type,
                      "source": source, "created_at": created_at})

    monkeypatch.setattr(repo, "upsert_subscriber", upsert_subscriber)
    return calls


def _client() -> TestClient:
    from api.main import app
    return TestClient(app)


def test_subscribe_normalizes_and_forwards(captured: list[dict]) -> None:
    r = _client().post("/alerts/subscribe", json={
        "email": "  Buyer@Example.COM ",
        "city": "Chennai",
        "property_type": "Plot",
        "source": "landing:chennai/plots",
    })
    assert r.status_code == 200
    assert r.json() == {"status": "subscribed"}
    assert len(captured) == 1
    row = captured[0]
    assert row["email"] == "buyer@example.com"  # trimmed + lowercased
    assert row["city"] == "Chennai"
    assert row["property_type"] == "Plot"
    assert row["source"] == "landing:chennai/plots"
    assert row["created_at"].endswith("Z")


def test_subscribe_email_only_is_valid(captured: list[dict]) -> None:
    r = _client().post("/alerts/subscribe", json={"email": "solo@example.com"})
    assert r.status_code == 200
    assert captured[0]["city"] is None
    assert captured[0]["property_type"] is None


def test_subscribe_rejects_bad_email(captured: list[dict]) -> None:
    r = _client().post("/alerts/subscribe", json={"email": "not-an-email"})
    assert r.status_code == 422
    assert captured == []  # validation fires before the repo is touched


def test_subscribe_blank_fields_become_null(captured: list[dict]) -> None:
    r = _client().post("/alerts/subscribe", json={
        "email": "x@example.com", "city": "   ", "property_type": ""})
    assert r.status_code == 200
    assert captured[0]["city"] is None
    assert captured[0]["property_type"] is None


def test_subscribe_caps_long_field(captured: list[dict]) -> None:
    r = _client().post("/alerts/subscribe", json={
        "email": "x@example.com", "city": "A" * 500})
    assert r.status_code == 200
    assert len(captured[0]["city"]) == 120
