"""
api/billing/repository.py
-------------------------
Webhook idempotency store on Neo4j (design D2).

Razorpay redelivers webhooks on any non-2xx (and sometimes on 2xx), for up to
~90 days. To activate a plan **exactly once** we record each processed
`event_id` and skip duplicates. Retention is **time-based, not a count cap**: a
count cap could evict an id still inside the redelivery window during a burst and
let a duplicate through. `mark_event_seen` is atomic (MERGE) and reports whether
this call was the first to see the id.

All Cypher leads with a unique prefix so the test stub (tests/api/conftest.py)
can route queries to its in-memory store.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from api.neo4j_client import run_query_async


def ttl_days() -> int:
    return int(os.environ.get("RAZORPAY_WEBHOOK_TTL_DAYS", "90"))


async def mark_event_seen(event_id: str) -> bool:
    """Atomically record `event_id`; return True iff this call created it.

    The uniqueness token trick keeps the create/match decision inside one atomic
    MERGE: `seen_at` is stamped only ON CREATE with the per-call `$token`, so a
    later duplicate (whose stored token differs) returns first_time=False. No
    read-then-write race.
    """
    token = datetime.now(timezone.utc).isoformat()
    expires = (datetime.now(timezone.utc) + timedelta(days=ttl_days())).isoformat()
    rows = await run_query_async(
        """
        MERGE (e:WebhookEvent {event_id: $event_id})
        ON CREATE SET e.seen_at = $token, e.expires_at = $expires
        RETURN e.seen_at = $token AS first_time
        """,
        {"event_id": event_id, "token": token, "expires": expires},
    )
    return bool(rows and rows[0].get("first_time"))


async def prune_expired_events() -> int:
    """Delete idempotency records past their TTL. Intended for a periodic
    maintenance call (the store is correct without it — pruning only bounds
    storage). Returns the number deleted."""
    now = datetime.now(timezone.utc).isoformat()
    rows = await run_query_async(
        """
        MATCH (e:WebhookEvent)
        WHERE e.expires_at < $now
        WITH e LIMIT 10000
        DETACH DELETE e
        RETURN count(e) AS deleted
        """,
        {"now": now},
    )
    return int(rows[0]["deleted"]) if rows else 0
