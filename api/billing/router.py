"""
api/billing/router.py
---------------------
Razorpay paid-tier endpoints.

- `POST /billing/order`   (verified user) — mint an order for the unlock price,
  tagged with the buyer's supabase_id.
- `POST /billing/webhook` (public, Razorpay → us) — the **sole** activation path:
  verify signature → dedupe event → grant the plan.
- `POST /billing/verify`  (verified user) — non-authoritative checkout-return
  check for UX only; reports whether the webhook has activated the plan yet.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from api.auth import repository as auth_repo
from api.auth import require_verified_user
from api.auth.dependencies import _user_to_out
from api.auth.schemas import UserOut
from api.billing import client, repository as billing_repo
from api.billing.schemas import CreateOrderOut, VerifyIn, VerifyOut

logger = logging.getLogger(__name__)
router = APIRouter()

# Event types that mean "money captured". We key activation off payment.captured
# (the authoritative capture signal); order.paid is accepted as a belt-and-braces
# fallback for the same order.
_ACTIVATING_EVENTS = {"payment.captured", "order.paid"}


@router.post("/billing/order", response_model=CreateOrderOut)
async def create_order(user: UserOut = Depends(require_verified_user)) -> CreateOrderOut:
    """Create a Razorpay order for the signed-in user. The supabase_id rides in
    `notes` so the webhook can recover the buyer without trusting the browser."""
    try:
        order = await client.create_order(
            receipt=f"unlock-{user.id}-{uuid.uuid4().hex[:12]}",
            notes={"supabase_id": user.id},
        )
    except client.BillingConfigError:
        logger.exception("billing not configured")
        raise HTTPException(status_code=503, detail="billing temporarily unavailable")
    except Exception:
        logger.exception("razorpay order creation failed for user=%s", user.id)
        raise HTTPException(status_code=502, detail="could not create payment order")
    return CreateOrderOut(
        order_id=order["id"],
        amount=order.get("amount", client.plan_amount()),
        currency=order.get("currency", client.plan_currency()),
        key_id=client.key_id(),
    )


def _extract_supabase_id(body: dict) -> str | None:
    """Pull the buyer's supabase_id back out of the order/payment notes."""
    payload = body.get("payload") or {}
    for kind in ("payment", "order"):
        entity = (payload.get(kind) or {}).get("entity") or {}
        sub = (entity.get("notes") or {}).get("supabase_id")
        if sub:
            return sub
    return None


@router.post("/billing/webhook")
async def webhook(request: Request) -> dict:
    """Razorpay webhook — the only place a plan is activated (D3).

    Order of operations matters: signature first (reject forgeries), then
    idempotency (dedupe redeliveries, D2), then grant. Always 200 on a verified
    event (even a duplicate or an irrelevant type) so Razorpay stops retrying;
    400 only on a bad signature.
    """
    raw = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    if not client.verify_webhook_signature(raw, signature):
        raise HTTPException(status_code=400, detail="invalid signature")

    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid payload")

    event = body.get("event") or ""
    if event not in _ACTIVATING_EVENTS:
        return {"status": "ignored", "event": event}

    # Razorpay's unique per-delivery id is the idempotency key; fall back to the
    # payment id so we still dedupe if the header is ever absent.
    event_id = request.headers.get("X-Razorpay-Event-Id") or _payment_id(body)
    if not event_id:
        logger.warning("billing webhook %s missing event id — skipping", event)
        return {"status": "ignored", "reason": "no event id"}

    if not await billing_repo.mark_event_seen(event_id):
        return {"status": "duplicate", "event_id": event_id}

    sub = _extract_supabase_id(body)
    if not sub:
        logger.error("billing webhook %s (%s) has no supabase_id in notes", event, event_id)
        return {"status": "accepted", "granted": False}

    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=client.plan_days())
    ).isoformat().replace("+00:00", "Z")
    granted = await auth_repo.grant_plan(sub, expires_at)
    if not granted:
        logger.error("billing webhook %s: grant_plan found no user for %s", event_id, sub)
        return {"status": "accepted", "granted": False}
    logger.info("billing: granted paid plan to %s until %s (event %s)", sub, expires_at, event_id)
    return {"status": "granted", "expires_at": expires_at}


def _payment_id(body: dict) -> str | None:
    entity = ((body.get("payload") or {}).get("payment") or {}).get("entity") or {}
    return entity.get("id")


@router.post("/billing/verify", response_model=VerifyOut)
async def verify(
    body: VerifyIn, user: UserOut = Depends(require_verified_user)
) -> VerifyOut:
    """Checkout-return verification — UX only, never grants (D3).

    Confirms the browser-supplied signature is genuine, then reports whether the
    webhook has *already* activated the plan. If not, the client shows "payment
    pending" and waits for the webhook.
    """
    if not client.verify_payment_signature(
        body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature
    ):
        raise HTTPException(status_code=400, detail="invalid payment signature")
    # Re-read the user so this reflects a webhook that may have landed between the
    # request's auth load and now.
    row = await auth_repo.get_user_by_id(user.id)
    fresh = _user_to_out({**row, "email_verified": user.email_verified}) if row else user
    return VerifyOut(
        status="paid" if fresh.tier == "paid" else "pending",
        tier=fresh.tier,
        plan_expires_at=fresh.plan_expires_at,
    )
