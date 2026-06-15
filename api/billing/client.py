"""
api/billing/client.py
---------------------
Thin Razorpay surface: order creation over the REST API plus the two HMAC
signature checks (webhook + verify-on-return). Implemented with `httpx` (already
a dependency) and stdlib `hmac`, so no extra SDK / CVE-audit surface.

Credentials are read from the environment **at call time** (not import) so the
app boots without them in dev/test and the test suite can monkeypatch the HTTP
call without real keys. Amount is in the smallest currency unit (paise for INR).
"""
from __future__ import annotations

import hashlib
import hmac
import os

import httpx

_RAZORPAY_ORDERS_URL = "https://api.razorpay.com/v1/orders"


class BillingConfigError(RuntimeError):
    """Raised when a Razorpay credential needed for the call is missing."""


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise BillingConfigError(f"{name} is not configured")
    return val


def key_id() -> str:
    return _require("RAZORPAY_KEY_ID")


def plan_amount() -> int:
    """Unlock price in the smallest currency unit (default ₹499 = 49900 paise)."""
    return int(os.environ.get("RAZORPAY_PLAN_AMOUNT", "49900"))


def plan_currency() -> str:
    return os.environ.get("RAZORPAY_PLAN_CURRENCY", "INR").strip() or "INR"


def plan_days() -> int:
    """How long a single unlock lasts (default 30 days)."""
    return int(os.environ.get("RAZORPAY_PLAN_DAYS", "30"))


async def create_order(receipt: str, notes: dict[str, str]) -> dict:
    """Create a Razorpay order for the configured unlock price.

    `notes` rides along on the order and is echoed back in the webhook payload —
    that's how the server recovers which `supabase_id` to grant on capture
    without trusting anything from the browser.
    """
    key, secret = key_id(), _require("RAZORPAY_KEY_SECRET")
    payload = {
        "amount": plan_amount(),
        "currency": plan_currency(),
        "receipt": receipt,
        "notes": notes,
        "payment_capture": 1,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(_RAZORPAY_ORDERS_URL, json=payload, auth=(key, secret))
    resp.raise_for_status()
    return resp.json()


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """Verify the `X-Razorpay-Signature` header over the raw webhook body.

    Razorpay signs the *exact* bytes it sent with the webhook secret (HMAC-SHA256,
    hex). We must hash the raw request body — re-serialising the parsed JSON would
    change whitespace/key order and break the check.
    """
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_payment_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """Verify the checkout-return signature (`order_id|payment_id`, HMAC-SHA256,
    key secret). Non-authoritative — used only for UX (design D3)."""
    secret = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
    if not secret or not signature:
        return False
    body = f"{order_id}|{payment_id}".encode()
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
