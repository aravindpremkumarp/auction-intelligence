"""Endpoint tests for /alerts (GET authed + POST by-ids paths).

The alerts repository is faked in-memory so these pin the router contract:
authed GET resolves the saved set, anonymous GET is empty (not 401),
anonymous POST computes over supplied ids, and the response is shaped +
sorted by the service.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import auth_header

NOW = datetime.now(timezone.utc)


def _iso(**kw) -> str:
    return (NOW + timedelta(**kw)).isoformat()


@pytest.fixture
def fake_repo(monkeypatch: pytest.MonkeyPatch) -> dict:
    """In-memory stand-in for api.alerts.repository.

    `deadlines` maps auction_id -> deadline ISO string; `saved` maps
    supabase_id -> [auction_id]. The fakes mirror the real queries: only
    properties with a (non-null) deadline are returned.
    """
    from api.alerts import repository as repo

    state = {
        "deadlines": {
            "a-urgent": _iso(hours=6),
            "a-soon": _iso(days=2),
            "a-far": _iso(days=40),
            "a-none": None,
        },
        "titles": {"a-urgent": "Urgent flat", "a-soon": "Soon plot"},
        "cities": {"a-urgent": "Chennai", "a-soon": "Madurai"},
        "saved": {},
    }

    def _row(aid: str) -> dict | None:
        if aid not in state["deadlines"] or state["deadlines"][aid] is None:
            return None
        return {
            "auction_id": aid,
            "title": state["titles"].get(aid),
            "city": state["cities"].get(aid),
            "deadline": state["deadlines"][aid],
        }

    async def deadlines_for_saved(sub: str) -> list[dict]:
        return [r for aid in state["saved"].get(sub, []) if (r := _row(aid))]

    async def deadlines_for_ids(ids: list[str]) -> list[dict]:
        seen: list[str] = []
        for i in ids or []:
            s = str(i).strip()
            if s and s not in seen:
                seen.append(s)
        return [r for aid in seen if (r := _row(aid))]

    monkeypatch.setattr(repo, "deadlines_for_saved", deadlines_for_saved)
    monkeypatch.setattr(repo, "deadlines_for_ids", deadlines_for_ids)
    return state


def _client() -> TestClient:
    from api.main import app
    return TestClient(app)


def test_get_alerts_anonymous_is_empty_not_401(fake_repo: dict) -> None:
    r = _client().get("/alerts")
    assert r.status_code == 200
    assert r.json() == {"alerts": [], "count": 0}


def test_get_alerts_resolves_saved_set_and_filters(fake_repo: dict) -> None:
    fake_repo["saved"]["sub-a1"] = ["a-far", "a-soon", "a-urgent", "a-none"]
    h = auth_header(sub="sub-a1", email="a1@x.com")
    body = _client().get("/alerts", headers=h).json()
    # Far-future and null-deadline drop out; soonest first.
    assert [a["auction_id"] for a in body["alerts"]] == ["a-urgent", "a-soon"]
    assert body["count"] == 2
    assert body["alerts"][0]["severity"] == "urgent"
    assert body["alerts"][0]["type"] == "deadline"


def test_get_alerts_is_per_user(fake_repo: dict) -> None:
    fake_repo["saved"]["sub-a2"] = ["a-urgent"]
    h_other = auth_header(sub="sub-a3", email="a3@x.com")
    assert _client().get("/alerts", headers=h_other).json() == {"alerts": [], "count": 0}


def test_post_alerts_by_ids_anonymous(fake_repo: dict) -> None:
    r = _client().post("/alerts", json={"auction_ids": ["a-soon", "a-far", "a-none"]})
    assert r.status_code == 200
    body = r.json()
    assert [a["auction_id"] for a in body["alerts"]] == ["a-soon"]
    assert body["count"] == 1


def test_post_alerts_empty_body_authed_falls_back_to_saved(fake_repo: dict) -> None:
    fake_repo["saved"]["sub-a4"] = ["a-urgent"]
    h = auth_header(sub="sub-a4", email="a4@x.com")
    body = _client().post("/alerts", json={"auction_ids": []}, headers=h).json()
    assert [a["auction_id"] for a in body["alerts"]] == ["a-urgent"]


def test_post_alerts_empty_body_anonymous_is_empty(fake_repo: dict) -> None:
    body = _client().post("/alerts", json={"auction_ids": []}).json()
    assert body == {"alerts": [], "count": 0}
