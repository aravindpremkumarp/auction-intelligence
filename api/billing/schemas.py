"""
api/billing/schemas.py
----------------------
Request/response models for the Razorpay billing endpoints.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class CreateOrderOut(BaseModel):
    """Everything the browser checkout needs to open Razorpay for this order."""
    order_id: str
    amount: int
    currency: str
    key_id: str


class VerifyIn(BaseModel):
    """Checkout success payload posted back by the browser (verify-on-return)."""
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class VerifyOut(BaseModel):
    # "paid" only when the webhook has already activated the plan; otherwise
    # "pending" — verify-on-return never grants entitlement on its own (D3).
    status: Literal["paid", "pending"]
    tier: Literal["free", "paid"]
    plan_expires_at: str | None = None
