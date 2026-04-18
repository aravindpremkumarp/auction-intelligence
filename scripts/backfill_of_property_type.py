"""
backfill_of_property_type.py
----------------------------
One-off backfill: add (AuctionProperty)-[:OF_PROPERTY_TYPE]->(PropertyType)
edges for every auction in data/tn_auction_data.jsonl.

Does not touch any other node property or relationship — it is safe to
run alongside enrichment-layer state (unlike re-running load_tn_to_neo4j.py
which overwrites scraped scalars that the verified_enriched stage later
corrected).

Run:  python scripts/backfill_of_property_type.py
"""

import json
import os
import time
from neo4j import GraphDatabase

# ── Connection (same creds as load_tn_to_neo4j.py) ───────────────────────────
NEO4J_URI      = "neo4j+s://cc513ea9.databases.neo4j.io"
NEO4J_USERNAME = "cc513ea9"
NEO4J_PASSWORD = "ZCgIWawTvdawFfPrwSPnl-kEQF-HEjFR4_iYI-mMT08"
NEO4J_DATABASE = "cc513ea9"

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
INPUT_FILE   = os.path.join(PROJECT_ROOT, "data", "tn_auction_data.jsonl")
BATCH_SIZE   = 200

BACKFILL_QUERY = """
UNWIND $rows AS r
MATCH (a:AuctionProperty {auction_id: r.auction_id})
MERGE (pt:PropertyType {name: r.property_type})
MERGE (a)-[:OF_PROPERTY_TYPE]->(pt)
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
            property_type = r.get("property_type")
            if not auction_id or not property_type:
                continue
            rows.append({
                "auction_id": str(auction_id),
                "property_type": str(property_type).strip(),
            })
    return rows


def main() -> None:
    rows = load_rows(INPUT_FILE)
    total = len(rows)
    print(f"Backfill candidates: {total:,} auctions with property_type")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    ingested = 0
    errors = 0
    t_start = time.time()

    with driver.session(database=NEO4J_DATABASE) as session:
        for i in range(0, total, BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            try:
                session.run(BACKFILL_QUERY, rows=batch)
                ingested += len(batch)
                elapsed = time.time() - t_start
                rate = ingested / elapsed if elapsed > 0 else 0
                pct = ingested / total * 100
                print(f"  [{ingested:>5}/{total}] {pct:5.1f}%  |  {rate:.0f} rec/s", end="\r")
            except Exception as e:
                errors += 1
                print(f"\n  [ERROR] batch {i}–{i+len(batch)}: {e}")

    driver.close()
    print(f"\n\n{'='*50}")
    print(f"  Edges backfilled : {ingested:,} / {total:,}")
    print(f"  Errors           : {errors}")
    print(f"  Time             : {time.time() - t_start:.1f}s")
    print(f"{'='*50}")
    print("\nVerify in Neo4j Browser:")
    print("  MATCH (a:AuctionProperty)-[:OF_PROPERTY_TYPE]->(pt) RETURN pt.name, count(a) ORDER BY count(a) DESC")


if __name__ == "__main__":
    main()
