"""
scripts/resolve_lots_from_extraction.py
-----------------------------------------
Give every listing on a multi-lot notice its `resolved_lot_key` using the
grounded-extraction lot match `pipeline/apply_extractions.py` already
computes to route its field/description writes, but never persists.

That match (reserve price, then EMD, then borrower-name token overlap, then
survey/door identifiers, then unique-remainder pairing — see
`pipeline.apply_extractions.match_lots_to_listings`) is strictly more
evidence than `pipeline/lot_resolution.py`'s reserve+borrower-only pass over
the `:Lot` graph (`scripts/resolve_lots.py`), and it reads the live
extraction directly rather than a possibly-stale graph copy of it. This
script writes ONLY `resolved_lot_key` (+ the matching `ResolutionDecision`)
from that match — not fields, not descriptions; those stay
`apply_extractions.py`'s job and carry their own, separate blast radius.

Overwrites any prior AUTOMATED lot-match verdict when the two disagree
(this matcher wins), never a human's — see `human_decided_lot_matches()`.

Runs over raw HTTPS (Neo4j Query API v2), not the pooled Bolt driver
`api.neo4j_client` uses — same reason `scripts/resolve_lots.py` does: not
every environment this needs to run in can reach Bolt's raw socket port.
The matching logic itself is imported straight from
`pipeline.apply_extractions` — only the I/O layer differs from that
module's own `run()`.

Usage:
    python -m scripts.resolve_lots_from_extraction --dry-run
    python -m scripts.resolve_lots_from_extraction

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

from pipeline.apply_extractions import (
    entities_with_corrections, group_lots, match_lots_to_listings,
    sole_claimants,
)
from pipeline.resolution_review import lot_match_key

FETCH_BATCH = 50
WRITE_BATCH = 500


# ── Neo4j over HTTPS (Query API v2) — identical shape to resolve_lots.py ────

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


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ── collect ──────────────────────────────────────────────────────────────

def _multi_lot_filenames() -> list[str]:
    """Documents with >1 `Lot` AND a grounded extraction — the only ones
    this script's write can ever touch (a single-lot notice needs no
    resolved_lot_key; `api/agent3/common.py::scope_of()` already reads it
    as lot-scoped). Filtering here, not after fetching, is what keeps this
    script from pulling the ~21MB of extraction_json the full corpus
    carries — only ~13MB, for ~656 documents, actually matters."""
    rows = nq("""
        MATCH (d:Document)-[:HAS_LOT]->(l:Lot)
        WITH d, count(l) AS lot_count
        WHERE lot_count > 1 AND d.extraction_json IS NOT NULL
        RETURN d.filename
    """)
    return [r[0] for r in rows if r[0]]


def fetch_work(filenames: list[str]) -> list[dict]:
    """[{filename, extraction_json, corrections_json, listings}] for this
    batch of filenames — listings shaped like
    pipeline.apply_extractions.fetch_work()'s, joined here in Python from
    flat rows instead of a nested Cypher collect() (avoidable risk over the
    HTTP query API; scripts/resolve_lots.py explains this same choice)."""
    doc_rows = nq("""
        UNWIND $filenames AS fn
        MATCH (d:Document {filename: fn})
        RETURN d.filename, d.extraction_json, d.extraction_corrections_json
    """, {"filenames": filenames})

    listing_rows = nq("""
        UNWIND $filenames AS fn
        MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document {filename: fn})
        RETURN d.filename, a.auction_id, a.reserve_price_num, a.emd_num,
               a.title, a.website_description
    """, {"filenames": filenames})
    aids = [r[1] for r in listing_rows if r[1]]
    borrower_rows = nq("""
        UNWIND $aids AS aid
        MATCH (a:AuctionProperty {auction_id: aid})-[:HAS_BORROWER]->(b:Borrower)
        RETURN a.auction_id, b.name
    """, {"aids": aids}) if aids else []

    borrowers_by_aid: dict[str, list[str]] = defaultdict(list)
    for aid, name in borrower_rows:
        if name:
            borrowers_by_aid[aid].append(name)

    listings_by_fn: dict[str, list[dict]] = defaultdict(list)
    for fn, aid, price, emd, title, website_desc in listing_rows:
        if not aid:
            continue
        listings_by_fn[fn].append({
            "aid": aid, "price": price, "emd": emd,
            "borrowers": borrowers_by_aid.get(aid, []),
            "id_text": f"{title or ''} {website_desc or ''}",
        })

    return [{
        "filename": fn, "extraction_json": ext_json,
        "corrections_json": corr_json,
        "listings": listings_by_fn.get(fn, []),
    } for fn, ext_json, corr_json in doc_rows]


def human_decided_lot_matches() -> set[str]:
    """Mirrors pipeline.apply_extractions.human_decided_lot_matches() —
    same query, over HTTPS instead of Bolt."""
    rows = nq("""
        MATCH (r:ResolutionDecision {kind: 'lot-match', verdict: 'approved'})
        WHERE r.decided_by IS NOT NULL
          AND NOT r.decided_by STARTS WITH 'system:'
        RETURN r.payload_json
    """)
    out: set[str] = set()
    for (payload_json,) in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except (TypeError, ValueError):
            continue
        aid = payload.get("auction_id")
        if aid:
            out.add(aid)
    return out


def current_lot_keys(aids: list[str]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for batch in chunked(aids, 1000):
        for aid, key in nq("""
            UNWIND $aids AS aid
            MATCH (p:AuctionProperty {auction_id: aid})
            RETURN p.auction_id, p.resolved_lot_key
        """, {"aids": batch}):
            out[aid] = key
    return out


# ── write ────────────────────────────────────────────────────────────────

def write_lot_matches(rows: list[dict]) -> int:
    """Same write shape as pipeline.apply_extractions.write_lot_matches():
    set resolved_lot_key + lot_resolved_at, delete any superseded lot-match
    decision for this auction_id (its key embeds the lot_key, so a changed
    pick is a new node), merge the new one in as decided_by
    'system:apply_extractions' — this script is that pipeline's match, just
    run over HTTPS."""
    if not rows:
        return 0
    for row in rows:
        row["decision_key"] = lot_match_key(row["aid"], row["lot_key"])
        row["payload"] = json.dumps(
            {"auction_id": row["aid"], "lot_key": row["lot_key"],
             "method": row["reason"]}, ensure_ascii=False)
    written = 0
    for batch in chunked(rows, WRITE_BATCH):
        nq("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.aid})
            SET a.resolved_lot_key = row.lot_key,
                a.lot_resolved_at  = datetime()
        """, {"rows": batch})
        nq("""
            UNWIND $rows AS row
            MATCH (old:ResolutionDecision {kind: 'lot-match'})
            WHERE old.key STARTS WITH ('lot-match:' + row.aid + '|')
              AND old.key <> row.decision_key
            DETACH DELETE old
        """, {"rows": batch})
        nq("""
            UNWIND $rows AS row
            MERGE (r:ResolutionDecision {key: row.decision_key})
            SET r.kind = 'lot-match', r.verdict = 'approved',
                r.payload_json = row.payload, r.decided_at = datetime(),
                r.decided_by = 'system:apply_extractions'
        """, {"rows": batch})
        written += len(batch)
    return written


# ── main ─────────────────────────────────────────────────────────────────

def run(*, dry_run: bool = False, limit: int | None = None) -> dict:
    filenames = _multi_lot_filenames()
    if limit:
        filenames = filenames[:limit]
    print(f"{len(filenames)} multi-lot document(s) with a grounded extraction")

    human_decided = human_decided_lot_matches()
    print(f"{len(human_decided)} listing(s) have a human-decided lot-match "
          f"— never touched")

    stats: dict[str, int] = defaultdict(int)
    candidates: dict[str, dict] = {}   # aid -> {lot_key, reason}
    human_skipped = 0

    for fn_batch in chunked(filenames, FETCH_BATCH):
        for w in fetch_work(fn_batch):
            ents = entities_with_corrections(w["extraction_json"],
                                             w.get("corrections_json"))
            if not ents:
                stats["empty_extraction"] += 1
                continue
            lots = group_lots(ents)
            if len(lots) <= 1:
                continue
            matches, unmatched = match_lots_to_listings(lots, w["listings"])
            for listing, lot, reason in matches:
                stats[f"match_{reason}"] += 1
            for _listing, reason in unmatched:
                stats[f"unmatched_{reason}"] += 1

            # One lot, one listing — see apply_extractions.sole_claimants.
            sole = sole_claimants(matches)
            stats["lot_key_dropped_claimed_by_several"] += len(matches) - len(sole)
            for listing, lot, reason in sole:
                if listing["aid"] in human_decided:
                    human_skipped += 1
                    continue
                candidates[listing["aid"]] = {
                    "lot_key": f"{w['filename']}#{lot['lot_index']}",
                    "reason": reason,
                }

    print(f"match/unmatch stats: {dict(stats)}")
    print(f"skipped (human-decided): {human_skipped}")

    current = current_lot_keys(list(candidates.keys()))
    new_rows, override_rows, unchanged = [], [], 0
    for aid, c in candidates.items():
        prev = current.get(aid)
        if prev == c["lot_key"]:
            unchanged += 1
            continue
        row = {"aid": aid, "lot_key": c["lot_key"], "reason": c["reason"]}
        (override_rows if prev else new_rows).append(row)

    print(f"\n{len(new_rows)} newly resolved, {len(override_rows)} override "
          f"a prior automated match, {unchanged} already agree")

    summary = {"documents": len(filenames), "human_skipped": human_skipped,
               "new": len(new_rows), "overridden": len(override_rows),
               "unchanged": unchanged, "by_reason": dict(stats)}
    if dry_run:
        print("\n[dry-run] nothing written")
        return summary

    wrote = write_lot_matches(new_rows + override_rows)
    print(f"\nwrote resolved_lot_key for {wrote} listing(s)")
    summary["written"] = wrote
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="resolve + preview only")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N multi-lot documents")
    args = ap.parse_args()
    run(dry_run=args.dry_run, limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
