"""
scripts/resolve_lots.py
------------------------
Give every listing on a multi-lot sale notice its specific lot, where the
data already says which one.

655 `Document`s in the corpus bundle more than one `Lot` — 1,988
`AuctionProperty` listings, roughly two-thirds of the portal — and
`api/agent3/common.py::scope_of()` marks every value on those listings
"notice-scoped" rather than "lot-scoped", because nothing links a listing to
its specific lot. Most of the time something does: every lot's `Auction` node
and every listing already carry their own `reserve_price_num`, an exact
numeric join nobody queries (see `pipeline/lot_resolution.py` for the
resolution rule).

This script writes each resolved listing's lot back to:

    p.resolved_lot_key    the matched Lot's lot_key
    p.lot_resolved_at     when resolution last ran (mirrors entity_resolved_at)

and records a `(:ResolutionDecision {kind:'lot-match', verdict:'approved'})`
node per resolution — `decided_by:'system:auto'` for the rule, so a future
human-reviewed match (Phase B, not this script) is visually distinguishable
in the same audit trail.

Only ever writes an *approved* decision. There is no rejection path here:
a listing this script cannot resolve is simply left alone, exactly today's
notice-scoped behavior — the review queue for those cases is Phase B.

Usage:
    python -m scripts.resolve_lots --dry-run     # resolve + preview
    python -m scripts.resolve_lots               # write

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
from collections import defaultdict

from pipeline.lot_resolution import resolve_lot
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


# ── collect ──────────────────────────────────────────────────────────────
#
# Five flat queries joined in Python, not one nested one — the same shape
# `api/agent3/get_property.py` uses for listing/document/lots. Nested
# collect()-of-maps values are avoidable risk over the HTTP query API; flat
# rows joined by a known key are not.

def _multi_lot_file_paths() -> list[str]:
    rows = nq("""
        MATCH (d:Document)-[:HAS_LOT]->(l:Lot)
        WITH d, count(l) AS lot_count
        WHERE lot_count > 1
        RETURN d.file_path
    """)
    return [r[0] for r in rows if r[0]]


def collect() -> tuple[list[dict], dict[str, list[dict]]]:
    """Return (listings, lots_by_file_path).

    ``listings``: ``[{"file_path", "auction_id", "reserve", "borrower"}]``,
    one per `AuctionProperty` on a multi-lot notice.

    ``lots_by_file_path``: ``{file_path: [{"lot_key", "reserve",
    "borrowers"}]}`` — every candidate lot on that notice.
    """
    paths = _multi_lot_file_paths()
    if not paths:
        return [], {}

    listing_rows = nq("""
        MATCH (p:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
        WHERE d.file_path IN $paths
        RETURN d.file_path, p.auction_id, p.reserve_price_num
    """, {"paths": paths})
    listing_borrower_rows = nq("""
        MATCH (p:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
        WHERE d.file_path IN $paths
        MATCH (p)-[:HAS_BORROWER]->(b:Borrower)
        RETURN p.auction_id, b.name
    """, {"paths": paths})
    lot_rows = nq("""
        MATCH (d:Document)-[:HAS_LOT]->(cl:Lot)
        WHERE d.file_path IN $paths
        OPTIONAL MATCH (cl)-[:OFFERED_IN]->(ca:Auction)
        RETURN d.file_path, cl.lot_key, ca.reserve_price_num
    """, {"paths": paths})
    lot_borrower_rows = nq("""
        MATCH (d:Document)-[:HAS_LOT]->(cl:Lot)
        WHERE d.file_path IN $paths
        MATCH (cl)-[:HAS_PARTY|TITLE_HELD_BY]->(b:Borrower)
        RETURN cl.lot_key, b.name
    """, {"paths": paths})

    # First non-empty borrower per listing/lot — a notice can name several
    # parties; the resolver only needs one representative string to match
    # against, same as `resolve_bank_names.collect()`'s "first named wins".
    listing_borrower: dict[str, str] = {}
    for auction_id, name in listing_borrower_rows:
        if name and auction_id not in listing_borrower:
            listing_borrower[auction_id] = name

    lot_borrowers: dict[str, list[str]] = defaultdict(list)
    for lot_key, name in lot_borrower_rows:
        if name:
            lot_borrowers[lot_key].append(name)

    lot_reserve: dict[str, float | None] = {}
    lots_by_file_path: dict[str, list[dict]] = defaultdict(list)
    for file_path, lot_key, reserve in lot_rows:
        if not lot_key:
            continue
        lot_reserve[lot_key] = reserve
        lots_by_file_path[file_path].append({
            "lot_key": lot_key, "reserve": reserve,
            "borrowers": lot_borrowers.get(lot_key, []),
        })

    listings = [{
        "file_path": file_path, "auction_id": auction_id, "reserve": reserve,
        "borrower": listing_borrower.get(auction_id),
    } for file_path, auction_id, reserve in listing_rows if auction_id]

    return listings, dict(lots_by_file_path)


# ── apply ────────────────────────────────────────────────────────────────

def write_back(resolved: list[dict]) -> int:
    """Write `resolved_lot_key` + a `lot-match` decision per resolved
    listing. `resolved` items: ``{"auction_id", "lot_key", "method",
    "reason"}``."""
    if not resolved:
        return 0
    rows = [{
        "auction_id": r["auction_id"], "lot_key": r["lot_key"],
        "decision_key": lot_match_key(r["auction_id"], r["lot_key"]),
        "payload": json.dumps({
            "auction_id": r["auction_id"], "lot_key": r["lot_key"],
            "method": r["method"], "reason": r["reason"],
        }, ensure_ascii=False),
    } for r in resolved]
    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        nq("""
            UNWIND $rows AS row
            MATCH (p:AuctionProperty {auction_id: row.auction_id})
            SET p.resolved_lot_key = row.lot_key,
                p.lot_resolved_at  = datetime()
        """, {"rows": batch})
        nq("""
            UNWIND $rows AS row
            MERGE (r:ResolutionDecision {key: row.decision_key})
            SET r.kind = 'lot-match', r.verdict = 'approved',
                r.payload_json = row.payload, r.decided_at = datetime(),
                r.decided_by = 'system:auto'
        """, {"rows": batch})
    return len(rows)


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
    """Write `resolved_lot_key` for every already-DECIDED listing whose
    property doesn't reflect it yet — this is what makes a human's pick in
    the review queue actually take effect.

    A `ResolutionDecision` alone changes nothing on the `AuctionProperty`
    node; the property is what every agent3 tool actually reads. Only the
    DELTA is written (current value compared against the decision), not
    every already-applied one on every run — re-touching 1,800+ correct
    properties on every "Apply my decisions" click would be both wasteful
    and would blur `lot_resolved_at` into meaning nothing.
    """
    if not approved:
        return 0
    ids = list(approved.keys())
    current: dict[str, str | None] = {}
    for i in range(0, len(ids), 1000):
        batch = ids[i:i + 1000]
        for aid, key in nq("""
            UNWIND $ids AS aid
            MATCH (p:AuctionProperty {auction_id: aid})
            RETURN p.auction_id, p.resolved_lot_key
        """, {"ids": batch}):
            current[aid] = key
    stale = [{"auction_id": aid, "lot_key": lot_key}
            for aid, lot_key in approved.items() if current.get(aid) != lot_key]
    if not stale:
        return 0
    for i in range(0, len(stale), 500):
        nq("""
            UNWIND $rows AS row
            MATCH (p:AuctionProperty {auction_id: row.auction_id})
            SET p.resolved_lot_key = row.lot_key,
                p.lot_resolved_at  = datetime()
        """, {"rows": stale[i:i + 500]})
    return len(stale)


def run(*, dry_run: bool = False) -> dict:
    """One full resolution pass; the CLI and the review app's "Apply my
    decisions" button both land here. Returns a summary the caller can
    print or store.

    Two things happen, in order: every already-decided listing (auto or
    human) is applied to the graph if it isn't already, THEN the rule tries
    to auto-resolve whatever nobody has decided on at all.
    """
    listings, lots_by_file_path = collect()
    decisions = load_decisions()
    approved = _approved_lot_keys(decisions)

    pending = [l for l in listings if l["auction_id"] not in approved]
    print(f"{len(listings)} listing(s) on multi-lot notices; "
          f"{len(approved)} already decided, {len(pending)} to try")

    resolved: list[dict] = []
    by_method: dict[str, int] = defaultdict(int)
    unresolved = 0
    for listing in pending:
        candidates = lots_by_file_path.get(listing["file_path"], [])
        result = resolve_lot(listing_reserve=listing["reserve"],
                             listing_borrower=listing["borrower"],
                             candidates=candidates)
        if result["lot_key"] is None:
            unresolved += 1
            continue
        by_method[result["method"]] += 1
        resolved.append({"auction_id": listing["auction_id"],
                         "lot_key": result["lot_key"],
                         "method": result["method"],
                         "reason": result["reason"]})

    print(f"\nresolved {len(resolved)} of {len(pending)} attempted:")
    for method, n in sorted(by_method.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4}  {method}")
    print(f"  {unresolved:>4}  left ambiguous (no decision written)")

    summary = {"listings": len(listings), "already_decided": len(approved),
               "attempted": len(pending), "resolved": len(resolved),
               "by_method": dict(by_method), "unresolved": unresolved}
    if dry_run:
        print("\n[dry-run] nothing written")
        return summary

    applied = apply_decided(approved)
    wrote = write_back(resolved)
    print(f"\napplied {applied} previously-decided listing(s) to the graph "
          f"(human picks not yet reflected); "
          f"auto-resolved {wrote} new listing(s) this run")
    summary["applied"] = applied
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="resolve + preview only")
    args = ap.parse_args()
    run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
