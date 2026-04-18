"""
restore_has_property_type.py
----------------------------
Repair the production Neo4j graph after PR #5 (property_type per-auction
edge fix) was merged but never fully deployed.

Observed state before:
  HAS_PROPERTY_TYPE edges : 0      (schema the current tool code expects)
  OF_PROPERTY_TYPE edges  : 3,359  (stray edges from an earlier backfill)
  HAS_TYPE edges          : 0      (legacy, already cleaned up)

Result: every `property_type` filter in search_auctions / price_comparison /
get_auction_detail returns 0 rows on prod.

This script:
  1. Reads data/tn_auction_data.jsonl, splits each row's `property_type`
     on commas (mirroring scripts/prepare_tn_data.py), and creates
     (AuctionProperty)-[:HAS_PROPERTY_TYPE]->(PropertyType) edges.
  2. Deletes the stray (AuctionProperty)-[:OF_PROPERTY_TYPE]->(PropertyType)
     edges so the graph only contains the edge type the tools use.

Idempotent: safe to re-run.

Run:  python scripts/restore_has_property_type.py
"""

import json
import os
import time
from neo4j import GraphDatabase

from scripts.load_tn_to_neo4j import (
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE,
)

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
INPUT_FILE   = os.path.join(PROJECT_ROOT, "data", "tn_auction_data.jsonl")
BATCH_SIZE   = 200

POPULATE_QUERY = """
UNWIND $rows AS r
MATCH (a:AuctionProperty {auction_id: r.auction_id})
UNWIND r.property_types AS pt_name
WITH a, pt_name
WHERE pt_name IS NOT NULL AND pt_name <> ''
MERGE (pt:PropertyType {name: pt_name})
MERGE (a)-[:HAS_PROPERTY_TYPE]->(pt)
"""

DROP_STRAY_QUERY = """
MATCH (:AuctionProperty)-[r:OF_PROPERTY_TYPE]->(:PropertyType)
DELETE r
RETURN count(r) AS deleted
"""


def load_rows(path: str) -> list[dict]:
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            auction_id = r.get("auction_id")
            raw = r.get("property_type") or ""
            types = [p.strip() for p in raw.split(",") if p.strip()]
            if auction_id and types:
                rows.append({
                    "auction_id": str(auction_id),
                    "property_types": types,
                })
    return rows


def main() -> None:
    rows = load_rows(INPUT_FILE)
    print(f"Backfill candidates: {len(rows):,} auctions with property_type(s)")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    t_start = time.time()
    ingested = 0

    with driver.session(database=NEO4J_DATABASE) as session:
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            session.run(POPULATE_QUERY, rows=batch)
            ingested += len(batch)
            print(f"  HAS_PROPERTY_TYPE: [{ingested}/{len(rows)}]", end="\r")

        print(f"\n  HAS_PROPERTY_TYPE: populated for {ingested} auctions")

        deleted = session.run(DROP_STRAY_QUERY).single()["deleted"]
        print(f"  OF_PROPERTY_TYPE: {deleted} stray edges deleted")

        final = session.run(
            "MATCH (:AuctionProperty)-[r:HAS_PROPERTY_TYPE]->(:PropertyType) RETURN count(r) AS n"
        ).single()["n"]
        missing = session.run(
            "MATCH (a:AuctionProperty) WHERE NOT (a)-[:HAS_PROPERTY_TYPE]->() RETURN count(a) AS n"
        ).single()["n"]
        print(f"\nFinal state:")
        print(f"  HAS_PROPERTY_TYPE edges : {final}")
        print(f"  Auctions w/o HAS_PT edge: {missing}")

    driver.close()
    print(f"\nTime: {time.time() - t_start:.1f}s")


if __name__ == "__main__":
    main()
