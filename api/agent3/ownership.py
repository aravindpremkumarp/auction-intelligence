"""
api/agent3/ownership.py
-----------------------
Who a chat thread belongs to.

agent3's memory lives in `(:Conversation {id})-[:HAS_CHECKPOINT]->(:Checkpoint)`
keyed by `thread_id` **and nothing else** (see `api/checkpointer.py`). While
the endpoints were admin-only that was safe. It stops being safe the moment
anyone can call them: a thread id is the whole secret, and
`/chat/agent3/{id}/history` would hand one person's conversation to whoever
types their id.

So a thread gets an owner on first use and keeps it:

- **Claim on first use.** The first turn on a thread stamps `owner_key` onto
  the `Conversation` node. `MERGE` + `ON CREATE`-style write, done in one
  atomic Cypher statement so two first turns racing cannot both win.
- **Anonymous threads have owners too.** The key is the same salted IP hash
  the anonymous chat quota already uses (`api/chat/gating.anon_quota_key`) —
  one salt, one definition. It is weaker than an account (an IP moves, and a
  shared NAT collides), but it is the identity we have for a logged-out
  visitor and it is far better than the nothing it replaces.
- **A mismatch never errors on a POST.** It mints a fresh thread instead:
  losing one turn's memory is a far better failure than refusing to answer,
  which is the same rule `router._thread_id` already applies to a malformed
  id. On the read and delete endpoints a mismatch is a 404 — not a 403,
  because a 403 would confirm the thread exists.

Signing in does NOT inherit an anonymous thread. The browser mints a new id
on login anyway, and quietly re-keying a thread from an IP hash to an account
would let anyone on that NAT claim a conversation by logging in at the right
moment.
"""
from __future__ import annotations

from api.auth.schemas import UserOut
from api.neo4j_client import run_query_async

#: Marks a thread owned by an account, against one owned by an IP hash. The
#: prefix is what stops a supabase_id and a hash ever colliding.
_USER_PREFIX = "user:"
_ANON_PREFIX = "anon:"


def owner_key(request, user: UserOut | None) -> str:
    """The identity this caller's threads are keyed to."""
    if user is not None:
        return f"{_USER_PREFIX}{user.id}"
    # Imported here rather than at module scope: `api/chat/__init__.py` pulls
    # in the FastAPI router, and `api/agent3` must stay importable with
    # nothing but the Neo4j driver (its evals and tool tests run that way).
    from api.chat.gating import anon_quota_key

    return f"{_ANON_PREFIX}{anon_quota_key(request)}"


_CLAIM_CYPHER = """
MERGE (c:Conversation {id: $tid})
  ON CREATE SET c.owner_key = $key, c.owner_claimed_at = datetime()
WITH c
// An older thread predates this field. Claiming it for the caller would let
// whoever asks first take someone else's conversation, so an unowned thread
// is claimed ONLY when it has no checkpoints yet — i.e. nothing to steal.
FOREACH (_ IN CASE WHEN c.owner_key IS NULL
                    AND NOT (c)-[:HAS_CHECKPOINT]->() THEN [1] ELSE [] END |
  SET c.owner_key = $key, c.owner_claimed_at = datetime())
RETURN c.owner_key AS owner
"""


async def claim(thread_id: str, key: str) -> bool:
    """Take ownership of `thread_id`, or confirm the caller already has it.

    True when the thread is the caller's to write to. False when it belongs
    to someone else — the caller gets a fresh thread, never an error.

    Fails **closed** on a database error: an unreachable graph must not turn
    into "sure, it's yours". The caller reads that as "not mine", mints a new
    thread and answers, so an outage costs memory rather than privacy.
    """
    try:
        rows = await run_query_async(_CLAIM_CYPHER, {"tid": thread_id, "key": key})
    except Exception:  # noqa: BLE001 - privacy over convenience, see docstring
        return False
    owner = rows[0].get("owner") if rows else None
    # A thread still carrying no owner is a pre-existing one with checkpoints
    # on it. Nobody may write to it until its real owner is known, which no
    # migration can recover — so it reads as someone else's.
    return owner == key


_OWNS_CYPHER = """
MATCH (c:Conversation {id: $tid})
RETURN c.owner_key AS owner
"""


async def owns(thread_id: str, key: str) -> bool:
    """Read-only ownership check, for the history / manifest / delete paths.

    A thread nobody has claimed is nobody's to read. Fails closed for the
    same reason `claim` does.
    """
    try:
        rows = await run_query_async(_OWNS_CYPHER, {"tid": thread_id})
    except Exception:  # noqa: BLE001 - privacy over convenience
        return False
    if not rows:
        return False
    return rows[0].get("owner") == key
