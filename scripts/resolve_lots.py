"""
scripts/resolve_lots.py
------------------------
Apply decided lot matches to the graph. This is what the review app's
"Apply my decisions" button runs.

A `(:ResolutionDecision {kind:'lot-match', verdict:'approved'})` node on its
own changes nothing an agent tool can read — `AuctionProperty` is what every
agent3 tool actually queries. This script writes the delta:

    p.resolved_lot_key    the decided Lot's lot_key
    p.lot_resolved_at     when it was applied

RETIRED: this script used to also AUTO-RESOLVE undecided listings, matching
on reserve price and borrower name via `pipeline.lot_resolution.resolve_lot`
and writing `decided_by='system:auto'`. That half is gone, because it had no
rivalry gate — nothing stopped several listings landing on the same lot. The
corpus shows the cost: of the 100 keys it ever wrote, 96 sat on a lot another
listing also claimed (116 listings across 50 lots, one lot claimed by seven).
Two listings cannot both be that lot, so those listings were showing each
other's property.

`pipeline/apply_extractions.py::write_lot_matches` is the only resolver now.
It weighs more evidence (reserve, then EMD, then borrower, then survey/door
identifiers), reads the live extraction rather than a graph copy of it, and
`sole_claimants` makes it decline to write a lot two listings claim.

`resolve_lot` itself is untouched and still used: `api/review/queries.py`
calls it read-only to show a reviewer the candidate lots and the evidence the
rule compared. Showing a human what it saw is not the same as writing its guess.

Usage:
    python -m scripts.resolve_lots --dry-run     # preview
    python -m scripts.resolve_lots               # apply

Auth: NEO4J_URI/USERNAME/PASSWORD(/DATABASE).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.request

from pipeline.resolution_review import lot_match_key
from scripts.resolution_decisions import load_decisions


# ── Neo4j over HTTPS (Query API v2) ─────────────────────────────────────────

def _endpoint() -> tuple[str, str]:
    host = os.environ["NEO4J_URI"].split("//", 1)[1].rstrip("/")
    db = os.environ.get("NEO4J_DATABASE", "neo4j")
    auth = base64.b64encode(
        f'{os.environ["NEO4J_USERNAME"]}:{os.environ["NEO4J_PASSWORD"]}'.encode()
    ).decode()
    return f"https://{host}/db/{db}/query/v2", auth


def nq(statement: str, parameters: dict | None = None) -> list[list]:
    url, auth = _endpoint()
    body = json.dumps({"statement": statement,
                       "parameters": parameters or {}}).encode()
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Authorization": "Basic " + auth,
                         "Content-Type": "application/json",
                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)["data"]["values"]
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


# ── apply decided matches ────────────────────────────────────────────────
#
# Five flat queries joined in Python, not one nested one — the same shape
# `api/agent3/get_property.py` uses for listing/document/lots. Nested
# collect()-of-maps values are avoidable risk over the HTTP query API; flat
# rows joined by a known key are not.

def _approved_lot_keys(decisions: list[dict]) -> dict[str, str]:
    """``auction_id -> lot_key`` for every approved lot-match decision,
    whoever made it. A human's pick from the review queue and the
    resolver's own auto-approval are indistinguishable once decided — both
    get applied to the graph the same way, by :func:`apply_decided`.
    """
    out: dict[str, str] = {}
    for d in decisions:
        if d.get("kind") != "lot-match" or d.get("verdict") != "approved":
            continue
        payload = d.get("payload") or {}
        aid, lot_key = payload.get("auction_id"), payload.get("lot_key")
        if aid and lot_key:
            out[aid] = lot_key
    return out


def apply_decided(approved: dict[str, str]) -> int:
    """Link every already-DECIDED listing whose edge doesn't reflect its
    verdict yet — this is what makes a human's pick in the review queue
    actually take effect.

    A `ResolutionDecision` alone creates no
    `(:AuctionProperty)-[:IS_LOT]->(:Lot)` edge; that edge is what every
    agent3 tool actually reads. Only the
    DELTA is written (current value compared against the decision), not
    every already-applied one on every run — re-touching 1,800+ correct
    edges on every "Apply my decisions" click would be both wasteful and
    would blur `linked_at` into meaning nothing.
    """
    if not approved:
        return 0
    ids = list(approved.keys())
    current: dict[str, str | None] = {}
    for i in range(0, len(ids), 1000):
        batch = ids[i:i + 1000]
        # Phase 4: the edge is the resolution, so the current value is the
        # lot it points at rather than a string stored on the listing.
        for aid, key in nq("""
            UNWIND $ids AS aid
            MATCH (p:AuctionProperty {auction_id: aid})
            OPTIONAL MATCH (p)-[:IS_LOT]->(l:Lot)
            RETURN p.auction_id, l.lot_key
        """, {"ids": batch}):
            current[aid] = key
    stale = [{"auction_id": aid, "lot_key": lot_key}
            for aid, lot_key in approved.items() if current.get(aid) != lot_key]
    if not stale:
        return 0
    for i in range(0, len(stale), 500):
        # A decision replaces whatever edge is there: one listing is one lot,
        # so the old edge goes before the new one is made. `method` says the
        # edge came from a stored verdict rather than from the matcher —
        # _approved_lot_keys deliberately does not record WHO decided, so
        # nothing here may claim to.
        nq("""
            UNWIND $rows AS row
            MATCH (p:AuctionProperty {auction_id: row.auction_id})
            MATCH (l:Lot {lot_key: row.lot_key})
            OPTIONAL MATCH (p)-[old:IS_LOT]->(:Lot)
            DELETE old
            MERGE (p)-[r:IS_LOT]->(l)
            SET r.linked_at = datetime(), r.method = 'decision'
        """, {"rows": stale[i:i + 500]})
    return len(stale)


def run(*, dry_run: bool = False) -> dict:
    """Apply every already-decided lot match to the graph. The CLI and the
    review app's "Apply my decisions" button both land here.

    This used to ALSO auto-resolve undecided listings with
    `pipeline.lot_resolution.resolve_lot` (reserve price + borrower name),
    writing `resolved_lot_key` under `decided_by='system:auto'`. That half is
    gone. It had no rivalry gate, so nothing stopped several listings resolving
    onto one lot, and the graph shows what that cost: of the 100 keys it ever
    wrote, 96 were on a lot another listing also claimed — 116 listings sharing
    50 lots, one lot claimed by seven. Two listings on one lot cannot both be
    right, so those listings were showing each other's property.

    `pipeline/apply_extractions.py::write_lot_matches` is the single resolver
    now. It compares more evidence (reserve, then EMD, then borrower, then
    survey/door identifiers) against the live extraction rather than a graph
    copy of it, and `sole_claimants` makes it refuse to write a lot two
    listings claim — for the reason the corpus just demonstrated.

    `resolve_lot` itself stays: `api/review/queries.py` calls it read-only to
    show a human the candidate lots and the evidence the rule compared. Showing
    a reviewer what it saw is a different thing from writing its guess.
    """
    decisions = load_decisions()
    approved = _approved_lot_keys(decisions)
    print(f"{len(approved)} decided lot match(es) on record")
    if dry_run:
        print("[dry-run] nothing written")
        return {"already_decided": len(approved), "applied": 0}
    applied = apply_decided(approved)
    print(f"applied {applied} previously-decided listing(s) to the graph "
          f"(picks not yet reflected)")
    return {"already_decided": len(approved), "applied": applied}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="resolve + preview only")
    args = ap.parse_args()
    run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
