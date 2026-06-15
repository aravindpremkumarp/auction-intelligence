"""
tests/e2e/test_razorpay_live.py
-------------------------------
Live test-mode E2E + real-Neo4j concurrency (design CQ2 / D4 / D5).

These run only when real credentials are present (see conftest's gate). They use
the smallest meaningful real interactions:
- a genuine Razorpay test-mode order over the network (proves keys + REST path),
- a real-signature webhook grant written to and read back from a real Neo4j,
- the atomic quota counter under genuine DB concurrency (D4).

Each test provisions a uniquely-named throwaway :User and deletes it in a
finally block so the shared test database stays clean. All Neo4j work for a test
runs inside a single event loop (one asyncio.run per test) and disposes the
cached async driver at the end — the driver binds connections to the loop it was
created on, so reusing it across loops raises "attached to a different loop".
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from api.auth import repository as auth_repo
from api.billing import client
from api.billing import repository as billing_repo
from api.neo4j_client import run_query_async


def _sign(raw: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


async def _delete_user(sub: str) -> None:
    await run_query_async("MATCH (u:User {supabase_id: $s}) DETACH DELETE u", {"s": sub})


async def _delete_event(event_id: str) -> None:
    await run_query_async("MATCH (e:WebhookEvent {event_id: $e}) DETACH DELETE e", {"e": event_id})


async def _shutdown_driver() -> None:
    """Dispose the cached async driver so the next test's asyncio.run() (a fresh
    loop) builds a new one bound to that loop."""
    import api.neo4j_client as nc

    drv = getattr(nc, "_async_driver", None)
    if drv is not None:
        await drv.close()
        nc._async_driver = None


def test_create_real_test_mode_order() -> None:
    """A real order against Razorpay test mode — exercises live keys + REST."""
    order = asyncio.run(
        client.create_order(receipt=f"e2e-{uuid.uuid4().hex[:8]}", notes={"supabase_id": "e2e"})
    )
    assert order["id"].startswith("order_")
    assert order["amount"] == client.plan_amount()
    assert order["currency"] == client.plan_currency()


def test_webhook_grant_against_real_neo4j() -> None:
    """Real-signature capture → real grant_plan write → read back paid expiry."""
    sub = f"e2e-{uuid.uuid4().hex[:10]}"
    event_id = f"evt-{sub}"

    async def _main() -> None:
        try:
            await auth_repo.upsert_user_from_supabase(sub, f"{sub}@e2e.test", "E2E")
            raw = json.dumps({
                "event": "payment.captured",
                "payload": {"payment": {"entity": {"id": "pay_e2e", "notes": {"supabase_id": sub}}}},
            }).encode()
            sig = _sign(raw, os.environ["RAZORPAY_WEBHOOK_SECRET"])
            assert client.verify_webhook_signature(raw, sig), "real webhook secret must verify"
            assert await billing_repo.mark_event_seen(event_id) is True
            assert await billing_repo.mark_event_seen(event_id) is False  # idempotent
            expires = (
                datetime.now(timezone.utc) + timedelta(days=client.plan_days())
            ).isoformat().replace("+00:00", "Z")
            assert await auth_repo.grant_plan(sub, expires) is not None
            row = await auth_repo.get_user_by_id(sub)
            assert row and row.get("plan_expires_at"), "plan expiry must persist in Neo4j"
        finally:
            await _delete_user(sub)
            await _delete_event(event_id)
            await _shutdown_driver()

    asyncio.run(_main())


def test_quota_concurrency_exactly_one_winner() -> None:
    """D4: 20 parallel increments with one slot left → exactly 1 within cap.

    The atomicity lives in the DB, so this only means anything against a real
    Neo4j: concurrent SETs must serialise into distinct sequential counts, with
    exactly one landing on the cap (allowed) and the other 19 above it (rejected).
    """
    sub = f"e2e-{uuid.uuid4().hex[:10]}"
    cap = 5
    bucket = "e2e-concurrency"

    async def _main() -> None:
        try:
            await auth_repo.upsert_user_from_supabase(sub, f"{sub}@e2e.test", "E2E")
            # Seed one slot remaining: count = cap - 1 in this bucket.
            await run_query_async(
                "MATCH (u:User {supabase_id: $s}) SET u.chat_count = $c, u.chat_bucket = $b",
                {"s": sub, "c": cap - 1, "b": bucket},
            )
            counts = list(await asyncio.gather(
                *[auth_repo.bump_chat_quota(sub, bucket) for _ in range(20)]
            ))
            allowed = [c for c in counts if c is not None and c <= cap]
            rejected = [c for c in counts if c is None or c > cap]
            assert len(allowed) == 1, f"expected exactly 1 winner, got {allowed} from {counts}"
            assert len(rejected) == 19, f"expected 19 rejects, got {len(rejected)} from {counts}"
            # Distinct sequential counts prove the increment was atomic (no lost updates).
            assert sorted(counts) == list(range(cap, cap + 20)), f"non-atomic counts: {counts}"
        finally:
            await _delete_user(sub)
            await _shutdown_driver()

    asyncio.run(_main())
