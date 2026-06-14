"""Endpoint tests for /dossiers.

The repository is faked in-memory; these pin the router contract: auth gating,
create validation, the readiness checklist on responses, cascade-delete, and
per-user isolation (another user's dossier_id 404s, never leaks).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import auth_header


@pytest.fixture
def fake_repo(monkeypatch: pytest.MonkeyPatch) -> dict:
    from api.dossier import repository as repo

    state: dict = {
        "auctions": {"a-1", "a-2"},     # auctions that "exist" in the graph
        "dossiers": {},                  # dossier_id -> record (incl. owner)
    }

    async def create_dossier_for_auction(sub, did, title, aid):
        if aid not in state["auctions"]:
            return None
        rec = {
            "owner": sub, "id": did, "title": title,
            "created_at": "t0", "updated_at": "t0",
            "property": {"kind": "auction_property", "auction_id": aid, "label": None},
            "documents": [],
        }
        state["dossiers"][did] = rec
        return {k: rec[k] for k in ("id", "title", "created_at", "updated_at", "property")}

    async def create_dossier_for_user_property(sub, did, title, pid, *, label,
                                               survey_no, sub_registrar, address):
        rec = {
            "owner": sub, "id": did, "title": title,
            "created_at": "t0", "updated_at": "t0",
            "property": {"kind": "user_property", "id": pid, "label": label,
                         "survey_no": survey_no, "sub_registrar": sub_registrar,
                         "address": address},
            "documents": [],
        }
        state["dossiers"][did] = rec
        return {k: rec[k] for k in ("id", "title", "created_at", "updated_at", "property")}

    async def list_dossiers(sub):
        out = []
        for rec in state["dossiers"].values():
            if rec["owner"] != sub:
                continue
            out.append({
                "id": rec["id"], "title": rec["title"],
                "created_at": rec["created_at"], "updated_at": rec["updated_at"],
                "property": rec["property"],
                "doc_count": len(rec["documents"]),
                "doc_types": [d.get("doc_type") for d in rec["documents"]],
            })
        return out

    async def get_dossier(sub, did):
        rec = state["dossiers"].get(did)
        if rec is None or rec["owner"] != sub:
            return None
        return {
            "id": rec["id"], "title": rec["title"],
            "created_at": rec["created_at"], "updated_at": rec["updated_at"],
            "property": rec["property"], "documents": list(rec["documents"]),
        }

    async def get_dossier_r2_keys(sub, did):
        rec = state["dossiers"].get(did)
        if rec is None or rec["owner"] != sub:
            return None
        return [d["r2_key"] for d in rec["documents"] if d.get("r2_key")]

    async def delete_dossier(sub, did):
        rec = state["dossiers"].get(did)
        if rec is None or rec["owner"] != sub:
            return False
        del state["dossiers"][did]
        return True

    monkeypatch.setattr(repo, "create_dossier_for_auction", create_dossier_for_auction)
    monkeypatch.setattr(repo, "create_dossier_for_user_property", create_dossier_for_user_property)
    monkeypatch.setattr(repo, "list_dossiers", list_dossiers)
    monkeypatch.setattr(repo, "get_dossier", get_dossier)
    monkeypatch.setattr(repo, "get_dossier_r2_keys", get_dossier_r2_keys)
    monkeypatch.setattr(repo, "delete_dossier", delete_dossier)
    return state


def _client() -> TestClient:
    from api.main import app
    return TestClient(app)


def test_dossiers_require_auth(fake_repo: dict) -> None:
    client = _client()
    assert client.get("/dossiers").status_code == 401
    assert client.post("/dossiers", json={"auction_id": "a-1"}).status_code == 401
    assert client.get("/dossiers/x").status_code == 401
    assert client.delete("/dossiers/x").status_code == 401


def test_create_for_auction_returns_empty_checklist(fake_repo: dict) -> None:
    client = _client()
    h = auth_header(sub="d1", email="d1@x.com")
    r = client.post("/dossiers", json={"auction_id": "a-1"}, headers=h)
    assert r.status_code == 201
    body = r.json()
    assert body["property"]["auction_id"] == "a-1"
    assert body["checklist"]["score"] == {"score": 0, "have": 0, "total": 10}
    assert len(body["checklist"]["categories"]) == 9


def test_create_for_unknown_auction_404(fake_repo: dict) -> None:
    client = _client()
    h = auth_header(sub="d1", email="d1@x.com")
    r = client.post("/dossiers", json={"auction_id": "nope"}, headers=h)
    assert r.status_code == 404


def test_create_for_user_property(fake_repo: dict) -> None:
    client = _client()
    h = auth_header(sub="d1", email="d1@x.com")
    r = client.post(
        "/dossiers",
        json={"user_property": {"label": "Plot 4, Avadi", "survey_no": "123/2"}},
        headers=h,
    )
    assert r.status_code == 201
    assert r.json()["property"]["kind"] == "user_property"


def test_create_requires_exactly_one_target(fake_repo: dict) -> None:
    client = _client()
    h = auth_header(sub="d1", email="d1@x.com")
    # Neither.
    assert client.post("/dossiers", json={}, headers=h).status_code == 422
    # Both.
    both = {"auction_id": "a-1", "user_property": {"label": "X"}}
    assert client.post("/dossiers", json=both, headers=h).status_code == 422


def test_list_and_get_and_delete(fake_repo: dict) -> None:
    client = _client()
    h = auth_header(sub="d1", email="d1@x.com")
    did = client.post("/dossiers", json={"auction_id": "a-1"}, headers=h).json()["id"]

    listed = client.get("/dossiers", headers=h).json()["dossiers"]
    assert len(listed) == 1
    assert listed[0]["readiness"] == {"score": 0, "have": 0, "total": 10}

    got = client.get(f"/dossiers/{did}", headers=h)
    assert got.status_code == 200
    assert got.json()["checklist"]["score"]["score"] == 0

    assert client.delete(f"/dossiers/{did}", headers=h).status_code == 204
    assert client.get(f"/dossiers/{did}", headers=h).status_code == 404


def test_get_missing_dossier_404(fake_repo: dict) -> None:
    client = _client()
    h = auth_header(sub="d1", email="d1@x.com")
    assert client.get("/dossiers/does-not-exist", headers=h).status_code == 404
    assert client.delete("/dossiers/does-not-exist", headers=h).status_code == 404


def test_dossiers_are_per_user(fake_repo: dict) -> None:
    client = _client()
    h1 = auth_header(sub="owner", email="o@x.com")
    h2 = auth_header(sub="intruder", email="i@x.com")
    did = client.post("/dossiers", json={"auction_id": "a-1"}, headers=h1).json()["id"]

    # The intruder cannot see, fetch, or delete the owner's dossier.
    assert client.get("/dossiers", headers=h2).json() == {"dossiers": []}
    assert client.get(f"/dossiers/{did}", headers=h2).status_code == 404
    assert client.delete(f"/dossiers/{did}", headers=h2).status_code == 404
    # Owner still has it.
    assert client.get(f"/dossiers/{did}", headers=h1).status_code == 200
