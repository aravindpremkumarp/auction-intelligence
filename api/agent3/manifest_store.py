"""
api/agent3/manifest_store.py
----------------------------
Where a turn's manifest lives: next to the conversation it belongs to.

One `TurnManifest` node per final answer, keyed `(thread_id, turn_index)` and
hung off the same `Conversation` node `api/checkpointer.py` MERGEs for the
thread's checkpoints. That co-location is the point — retention is the
thread's lifetime, and `DELETE /chat/agent3/{thread_id}` clears both in one
call, preserving the one-clear-site rule the design doc fought for.

**Why the JSON string columns.** Neo4j properties cannot hold lists of maps,
so `card_rows`, `annotations`, `breakdown`, `query_echo` and `web_sources`
are each stored as one JSON string. This is also why `card_rows` is a
*snapshot of card fields only* and not full graph rows: the duplication is
deliberate — it freezes what the user was shown even if the listing later
changes — and bounding it is what keeps it affordable.

Every function here is best-effort at the call site, not here: this module
raises, and the router decides that a failed manifest write must not fail a
turn that already produced a good answer.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from api.agent3.manifest import TurnManifest
from api.neo4j_client import run_query_async, run_read_query_async

logger = logging.getLogger("api.agent3.manifest_store")

#: Cap on one manifest's serialised payload. A search can hold 500 rows of
#: card fields; this is the ceiling that keeps a single pathological turn from
#: writing a property the next read has to haul back. Over it, the rows are
#: dropped and the manifest is stored without them — an annotation-only turn
#: still renders its text, which beats storing nothing.
MAX_ROWS_BYTES = 512_000

def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _load(raw: Any, fallback: Any) -> Any:
    if raw in (None, ""):
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("manifest property was not valid json — using default")
        return fallback


async def save(manifest: TurnManifest) -> None:
    """Write one manifest, replacing any manifest at the same ordinal.

    MERGE on `(thread_id, turn_index)` rather than CREATE: a retried turn must
    overwrite its own record rather than leave two claiming the same ordinal.
    Two writers on one thread compute the same index and one silently wins —
    a real misalignment, logged where it happens (`api/agent3/router.py`) and
    recovered by the card-less rule, not by shifting ordinals to make the
    join lie.
    """
    rows_json = _dump(manifest.card_rows)
    if len(rows_json) > MAX_ROWS_BYTES:
        logger.warning(
            "manifest card_rows %d bytes over the %d cap — storing without "
            "rows, thread=%s turn=%s",
            len(rows_json), MAX_ROWS_BYTES, manifest.thread_id,
            manifest.turn_index)
        rows_json = _dump([])

    await run_query_async(
        """
        MERGE (conv:Conversation {id: $tid})
          ON CREATE SET conv.created_at = datetime(),
                        conv.updated_at = datetime()
        MERGE (m:TurnManifest {thread_id: $tid, turn_index: $idx})
          ON CREATE SET m.created_at = datetime()
        SET m.kind = $kind,
            m.card_rows = $card_rows,
            m.discussed_ids = $discussed_ids,
            m.annotations = $annotations,
            m.query_echo = $query_echo,
            m.counts = $counts,
            m.breakdown = $breakdown,
            m.web_sources = $web_sources,
            m.produced_at = datetime()
        MERGE (conv)-[:HAS_MANIFEST]->(m)
        """,
        {
            "tid": manifest.thread_id,
            "idx": int(manifest.turn_index),
            "kind": manifest.kind,
            "card_rows": rows_json,
            "discussed_ids": _dump(manifest.discussed_ids),
            "annotations": _dump(manifest.annotations),
            "query_echo": _dump(manifest.query_echo),
            "counts": _dump(manifest.counts),
            "breakdown": _dump(manifest.breakdown),
            "web_sources": _dump(manifest.web_sources),
        },
    )


async def load_thread(thread_id: str) -> list[dict]:
    """Every manifest for a thread, in turn order.

    The frontend joins these to the thread's history by `turn_index`, so the
    order matters and the gaps matter: a turn whose manifest was lost renders
    card-less rather than borrowing its neighbour's.
    """
    rows = await run_read_query_async(
        """
        MATCH (m:TurnManifest {thread_id: $tid})
        RETURN m.turn_index AS turn_index, m.kind AS kind,
               m.card_rows AS card_rows, m.discussed_ids AS discussed_ids,
               m.annotations AS annotations, m.query_echo AS query_echo,
               m.counts AS counts, m.breakdown AS breakdown,
               m.web_sources AS web_sources,
               toString(m.produced_at) AS produced_at
        ORDER BY m.turn_index ASC
        """,
        {"tid": thread_id},
    )
    out: list[dict] = []
    for r in rows or []:
        out.append({
            "thread_id": thread_id,
            "turn_index": int(r.get("turn_index") or 0),
            "kind": r.get("kind") or "none",
            "card_rows": _load(r.get("card_rows"), []),
            "discussed_ids": _load(r.get("discussed_ids"), []),
            "annotations": _load(r.get("annotations"), {}),
            "query_echo": _load(r.get("query_echo"), None),
            "counts": _load(r.get("counts"), {"total": 0, "shown": 0}),
            "breakdown": _load(r.get("breakdown"), None),
            "web_sources": _load(r.get("web_sources"), []),
            "produced_at": r.get("produced_at"),
        })
    return out


async def delete_thread(thread_id: str) -> None:
    """Drop a thread's manifests.

    Called from the checkpointer's `adelete_thread` so "forget this thread"
    stays one call rather than two a caller can get half-right.
    """
    await run_query_async(
        "MATCH (m:TurnManifest {thread_id: $tid}) DETACH DELETE m",
        {"tid": thread_id},
    )
