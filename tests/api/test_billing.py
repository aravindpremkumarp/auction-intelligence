"""
tests/api/test_billing.py
-------------------------
Razorpay billing surface against the faked client + signed payloads (design
CQ2 — the stub lane). Covers order creation, the webhook's
signature/idempotency/grant path, and the non-authoritative verify-on-return.

The live test-mode E2E and the real-Neo4j concurrency test (D4/D5) run in a
separate lane that requires Razorpay secrets; they are not part of this stub
suite.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import auth_header

WEBHOOK_SECRET = "whsec_test"
KEY_SECRET = "secret_test"
KEY_ID = "rzp_test_abc"


@pytest.fixture
def billing_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("RAZORPAY_KEY_ID", KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", KEY_SECRET)
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("RAZORPAY_PLAN_DAYS", "30")
    from api.main import app
    return TestClient(app)


def _sign(raw: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _capture_body(sub: str, payment_id: str = "pay_1") -> bytes:
    return json.dumps({
        "event": "payment.captured",
        "payload": {"payment": {"entity": {
            "id": payment_id,
            "notes": {"supabase_id": sub},
        }}},
    }).encode()


def _materialise_user(client: TestClient, sub: str, email: str) -> None:
    """Hit an authed endpoint so the :User row exists before the webhook grants."""
    r = client.get("/auth/me", headers=auth_header(sub=sub, email=email))
    assert r.status_code == 200


# ── order ────────────────────────────────────────────────────────────────
def test_create_order_tags_supabase_id(
    billing_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {}

    async def _fake_create_order(receipt: str, notes: dict) -> dict:
        captured["receipt"] = receipt
        captured["notes"] = notes
        return {"id": "order_123", "amount": 49900, "currency": "INR"}

    from api.billing import client as billing_client_mod
    monkeypatch.setattr(billing_client_mod, "create_order", _fake_create_order)

    r = billing_client.post("/billing/order", headers=auth_header(sub="buyer-1", email="b@x.com"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"order_id": "order_123", "amount": 49900, "currency": "INR", "key_id": KEY_ID}
    assert captured["notes"] == {"supabase_id": "buyer-1"}


def test_create_order_requires_auth(billing_client: TestClient) -> None:
    assert billing_client.post("/billing/order").status_code == 401


# ── webhook (the only activation path) ─────────────────────────────────────
def test_webhook_grants_plan_on_valid_capture(billing_client: TestClient) -> None:
    sub = "wh-grant"
    _materialise_user(billing_client, sub, "g@x.com")
    raw = _capture_body(sub)
    r = billing_client.post(
        "/billing/webhook", content=raw,
        headers={"X-Razorpay-Signature": _sign(raw, WEBHOOK_SECRET),
                 "X-Razorpay-Event-Id": "evt_1"},
    )
    assert r.status_code == 200 and r.json()["status"] == "granted", r.text
    me = billing_client.get("/auth/me", headers=auth_header(sub=sub, email="g@x.com")).json()
    assert me["tier"] == "paid" and me["plan_expires_at"]


def test_webhook_rejects_bad_signature(billing_client: TestClient) -> None:
    raw = _capture_body("wh-bad")
    r = billing_client.post(
        "/billing/webhook", content=raw,
        headers={"X-Razorpay-Signature": "deadbeef", "X-Razorpay-Event-Id": "evt_bad"},
    )
    assert r.status_code == 400


def test_webhook_is_idempotent(billing_client: TestClient) -> None:
    sub = "wh-dup"
    _materialise_user(billing_client, sub, "d@x.com")
    raw = _capture_body(sub)
    headers = {"X-Razorpay-Signature": _sign(raw, WEBHOOK_SECRET), "X-Razorpay-Event-Id": "evt_dup"}
    first = billing_client.post("/billing/webhook", content=raw, headers=headers)
    second = billing_client.post("/billing/webhook", content=raw, headers=headers)
    assert first.json()["status"] == "granted"
    assert second.json()["status"] == "duplicate"


def test_webhook_ignores_unrelated_event(billing_client: TestClient) -> None:
    raw = json.dumps({"event": "payment.failed", "payload": {}}).encode()
    r = billing_client.post(
        "/billing/webhook", content=raw,
        headers={"X-Razorpay-Signature": _sign(raw, WEBHOOK_SECRET), "X-Razorpay-Event-Id": "evt_x"},
    )
    assert r.status_code == 200 and r.json()["status"] == "ignored"


def test_webhook_without_supabase_id_does_not_grant(billing_client: TestClient) -> None:
    raw = json.dumps({
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": "pay_no_notes", "notes": {}}}},
    }).encode()
    r = billing_client.post(
        "/billing/webhook", content=raw,
        headers={"X-Razorpay-Signature": _sign(raw, WEBHOOK_SECRET), "X-Razorpay-Event-Id": "evt_nonotes"},
    )
    assert r.status_code == 200 and r.json() == {"status": "accepted", "granted": False}


# ── verify-on-return (UX only, never grants — D3) ──────────────────────────
def _payment_sig(order_id: str, payment_id: str) -> str:
    return _sign(f"{order_id}|{payment_id}".encode(), KEY_SECRET)


def test_verify_is_pending_before_webhook(billing_client: TestClient) -> None:
    sub = "verify-pending"
    _materialise_user(billing_client, sub, "v@x.com")
    payload = {
        "razorpay_order_id": "order_v", "razorpay_payment_id": "pay_v",
        "razorpay_signature": _payment_sig("order_v", "pay_v"),
    }
    r = billing_client.post("/billing/verify", json=payload, headers=auth_header(sub=sub, email="v@x.com"))
    assert r.status_code == 200, r.text
    body = r.json()
    # Valid signature, but the webhook hasn't activated the plan -> no grant.
    assert body["status"] == "pending" and body["tier"] == "free"


def test_verify_rejects_bad_signature(billing_client: TestClient) -> None:
    sub = "verify-bad"
    _materialise_user(billing_client, sub, "vb@x.com")
    payload = {
        "razorpay_order_id": "order_v", "razorpay_payment_id": "pay_v",
        "razorpay_signature": "nope",
    }
    r = billing_client.post("/billing/verify", json=payload, headers=auth_header(sub=sub, email="vb@x.com"))
    assert r.status_code == 400


def test_verify_reports_paid_after_webhook(billing_client: TestClient) -> None:
    sub = "verify-paid"
    _materialise_user(billing_client, sub, "vp@x.com")
    raw = _capture_body(sub, payment_id="pay_vp")
    billing_client.post(
        "/billing/webhook", content=raw,
        headers={"X-Razorpay-Signature": _sign(raw, WEBHOOK_SECRET), "X-Razorpay-Event-Id": "evt_vp"},
    )
    payload = {
        "razorpay_order_id": "order_vp", "razorpay_payment_id": "pay_vp",
        "razorpay_signature": _payment_sig("order_vp", "pay_vp"),
    }
    r = billing_client.post("/billing/verify", json=payload, headers=auth_header(sub=sub, email="vp@x.com"))
    assert r.json()["status"] == "paid" and r.json()["tier"] == "paid"


# ── idempotency store ──────────────────────────────────────────────────────
def test_mark_event_seen_dedupes() -> None:
    import asyncio
    from api.billing import repository as repo

    async def _run() -> list[bool]:
        return [await repo.mark_event_seen("evt_repo"), await repo.mark_event_seen("evt_repo")]

    assert asyncio.run(_run()) == [True, False]
