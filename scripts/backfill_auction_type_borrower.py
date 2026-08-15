"""
scripts/backfill_auction_type_borrower.py
------------------------------------------
Repairs AuctionProperty nodes that are missing (:AuctionType) and (:Borrower)
edges.

Why they are missing
--------------------
prepare_tn_data.py used to read the JSONL key ``Auction Type`` while newer
scrapes write ``AuctionType``, so those records reached the loader with
``auction_type = ''``. load_tn_to_neo4j.py filters on it::

    WITH a, r
    WHERE r.auction_type IS NOT NULL AND r.auction_type <> ''   <- row dropped
    MERGE (at:AuctionType {name: r.auction_type})
    WITH a, r                                                    <- unreachable
    MERGE (bw:Borrower {name: r.borrower_name})                  <- unreachable

A filtered-out row skips every clause after it, so an empty auction_type also
cost the Borrower edge. Bank is merged *before* the filter, which is why those
same properties kept their Bank.

prepare_tn_data.py now reads both spellings, so new loads are correct. This
script repairs the ones already in the graph — the loader itself will not,
because it skips auction_ids that already exist.

Idempotent (MERGE only, no deletes).

Run:
    python scripts/backfill_auction_type_borrower.py --dry-run
    python scripts/backfill_auction_type_borrower.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

from neo4j import GraphDatabase

from pipeline.config import (
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE,
)

JSONL_FILE = ROOT / "data" / "tn_auction_data.jsonl"
BATCH_SIZE = 200

FIND_BROKEN = """
MATCH (a:AuctionProperty)
WHERE NOT EXISTS { (a)-[:IS_AUCTION_TYPE]->() }
   OR NOT EXISTS { (a)-[:HAS_BORROWER]->() }
RETURN a.auction_id AS aid
"""

# Each MERGE is guarded independently so one empty value cannot suppress the
# other -- the exact failure this script exists to repair.
REPAIR = """
UNWIND $rows AS row
MATCH (a:AuctionProperty {auction_id: row.auction_id})
FOREACH (_ IN CASE WHEN row.auction_type <> '' THEN [1] ELSE [] END |
  MERGE (at:AuctionType {name: row.auction_type})
  MERGE (a)-[:IS_AUCTION_TYPE]->(at)
)
FOREACH (_ IN CASE WHEN row.borrower_name <> '' THEN [1] ELSE [] END |
  MERGE (bw:Borrower {name: row.borrower_name})
  MERGE (a)-[:HAS_BORROWER]->(bw)
)
"""

VERIFY = """
MATCH (a:AuctionProperty)
RETURN count(a) AS total,
       sum(CASE WHEN EXISTS { (a)-[:IS_AUCTION_TYPE]->() } THEN 1 ELSE 0 END) AS with_at,
       sum(CASE WHEN EXISTS { (a)-[:HAS_BORROWER]->()    } THEN 1 ELSE 0 END) AS with_bw
"""


def load_jsonl() -> dict[str, dict]:
    recs: dict[str, dict] = {}
    with open(JSONL_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            recs[r["auction_id"]] = r
    return recs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would be repaired without writing.")
    args = ap.parse_args()

    recs = load_jsonl()
    print(f"tn_auction_data.jsonl records : {len(recs):,}")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    with driver.session(database=NEO4J_DATABASE) as s:
        broken = [r["aid"] for r in s.run(FIND_BROKEN)]
        print(f"properties missing an edge    : {len(broken):,}")

        rows, no_source, no_values = [], [], []
        for aid in broken:
            rec = recs.get(aid)
            if rec is None:
                no_source.append(aid)
                continue
            at = (rec.get("auction_type") or "").strip()
            bw = (rec.get("borrower_name") or "").strip()
            if not at and not bw:
                no_values.append(aid)
                continue
            rows.append({"auction_id": aid, "auction_type": at, "borrower_name": bw})

        print(f"  repairable from JSONL       : {len(rows):,}")
        if no_source:
            print(f"  not in JSONL (skipped)      : {len(no_source):,}")
        if no_values:
            print(f"  JSONL has no values         : {len(no_values):,}")

        for r in rows[:3]:
            print(f"    {r['auction_id']} | {r['auction_type']} | {r['borrower_name'][:40]}")

        if args.dry_run:
            print("\n[dry-run] no writes.")
            return

        if not rows:
            print("\nNothing to repair.")
            return

        print()
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            s.run(REPAIR, rows=batch)
            done = min(i + BATCH_SIZE, len(rows))
            print(f"  [{done}/{len(rows)}] repaired")

        v = s.run(VERIFY).single()
        t = v["total"]
        print("\n" + "=" * 55)
        print(f"  AuctionProperty total : {t:,}")
        print(f"  with AuctionType      : {v['with_at']:,}  ({v['with_at']/t:.1%})")
        print(f"  with Borrower         : {v['with_bw']:,}  ({v['with_bw']/t:.1%})")
        print("=" * 55)

    driver.close()


if __name__ == "__main__":
    main()
