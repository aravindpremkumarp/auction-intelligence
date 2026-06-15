"""
api/billing
-----------
Razorpay one-time, time-boxed paid-tier unlock.

The browser checkout is convenience; the **webhook is the sole source of truth**
for activation (design D3). `/billing/order` mints a Razorpay order tagged with
the buyer's `supabase_id`; Razorpay's `payment.captured` webhook verifies the
signature, dedupes the event (D2), and calls `auth.repository.grant_plan` to set
`plan_expires_at` — the same field PR 1's tier derivation already reads.
"""
from __future__ import annotations

from api.billing.router import router

__all__ = ["router"]
