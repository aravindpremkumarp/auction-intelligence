"""
tracking/auction_tracker.py
---------------------------
Eight-state pipeline tracker for auction investments, adapted from career-ops.

States (linear progression with branching at outcome):

    DISCOVERED → SCORED → SHORTLISTED → RESEARCHING → BID_READY
                                                         ↓
                                                    BID_SUBMITTED
                                                         ↓
                                                    WON / LOST
                                                         ↓
                                                    COMPLETED

State transitions past SCORED require explicit user confirmation
(human-in-the-loop). The tracker persists to Neo4j (InvestmentTracker nodes)
and to a local TSV file for portability.
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from pipeline.config import TRACKING_TSV
from api.neo4j_client import run_query


STATES = [
    "DISCOVERED",
    "SCORED",
    "SHORTLISTED",
    "RESEARCHING",
    "BID_READY",
    "BID_SUBMITTED",
    "WON",
    "LOST",
    "COMPLETED",
]

# States that require explicit user confirmation before transitioning into.
CONFIRMATION_REQUIRED = {
    "SHORTLISTED", "RESEARCHING", "BID_READY",
    "BID_SUBMITTED", "WON", "LOST", "COMPLETED",
}

VALID_TRANSITIONS = {
    "DISCOVERED":    {"SCORED"},
    "SCORED":        {"SHORTLISTED", "DISCOVERED"},
    "SHORTLISTED":   {"RESEARCHING", "SCORED"},
    "RESEARCHING":   {"BID_READY", "SHORTLISTED"},
    "BID_READY":     {"BID_SUBMITTED", "RESEARCHING"},
    "BID_SUBMITTED": {"WON", "LOST"},
    "WON":           {"COMPLETED"},
    "LOST":          {"COMPLETED"},
    "COMPLETED":     set(),
}


class InvalidTransition(ValueError):
    pass


def get_state(auction_id: str) -> str | None:
    rows = run_query(
        "MATCH (t:InvestmentTracker {auction_id: $id}) RETURN t.state AS state",
        {"id": auction_id},
    )
    return rows[0]["state"] if rows else None


def transition(
    auction_id: str,
    new_state: str,
    notes: str = "",
    confirmed: bool = False,
) -> dict:
    if new_state not in STATES:
        raise InvalidTransition(f"Unknown state: {new_state}")

    current = get_state(auction_id) or "DISCOVERED"
    allowed = VALID_TRANSITIONS.get(current, set())
    if new_state not in allowed and new_state != current:
        raise InvalidTransition(f"Cannot move {auction_id} from {current} to {new_state}")

    if new_state in CONFIRMATION_REQUIRED and not confirmed:
        return {
            "status": "confirmation_required",
            "auction_id": auction_id,
            "from_state": current,
            "to_state": new_state,
            "message": f"Transition to {new_state} requires explicit user confirmation.",
        }

    run_query(
        """
        MERGE (t:InvestmentTracker {auction_id: $id})
        ON CREATE SET t.created_at = datetime()
        SET t.state = $state,
            t.notes = coalesce($notes, t.notes),
            t.updated_at = datetime()
        WITH t
        MATCH (a:AuctionProperty {auction_id: $id})
        MERGE (a)-[:TRACKED_BY]->(t)
        """,
        {"id": auction_id, "state": new_state, "notes": notes or None},
    )
    _append_tsv(auction_id, current, new_state, notes)
    return {
        "status": "transitioned",
        "auction_id": auction_id,
        "from_state": current,
        "to_state": new_state,
    }


def _append_tsv(auction_id: str, from_state: str, to_state: str, notes: str) -> None:
    path = Path(TRACKING_TSV)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        if new_file:
            w.writerow(["timestamp", "auction_id", "from_state", "to_state", "notes"])
        w.writerow([datetime.now().isoformat(), auction_id, from_state, to_state, notes])


def list_by_state(state: str, limit: int = 100) -> list[dict]:
    return run_query(
        """
        MATCH (a:AuctionProperty)-[:TRACKED_BY]->(t:InvestmentTracker {state: $state})
        RETURN a.auction_id AS auction_id, a.title AS title,
               t.composite_score AS score, t.grade AS grade, t.updated_at AS updated_at
        ORDER BY t.composite_score DESC
        LIMIT $limit
        """,
        {"state": state, "limit": limit},
    )


def pipeline_summary() -> list[dict]:
    return run_query(
        """
        MATCH (t:InvestmentTracker)
        RETURN t.state AS state, count(t) AS count
        ORDER BY count DESC
        """
    )
