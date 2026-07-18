"""
load_tn_to_neo4j.py
--------------------
Loads tn_auction_data.jsonl into Neo4j Aura (cc513ea9).
Follows the auction_graph_model.json schema:

  (AuctionProperty)-[:CONDUCTED_BY]->(Bank)
  (Bank)-[:HAS_BRANCH]->(Branch)
  (AuctionProperty)-[:LISTED_BY_BRANCH]->(Branch)
  (AuctionProperty)-[:LOCATED_IN_CITY]->(City)
  (AuctionProperty)-[:LOCATED_IN_STATE]->(State)
  (AuctionProperty)-[:LOCATED_IN_AREA]->(Area)
  (Area)-[:PART_OF_CITY]->(City)
  (City)-[:IN_STATE]->(State)
  (AuctionProperty)-[:HAS_ASSET_CATEGORY]->(AssetCategory)
  (AuctionProperty)-[:HAS_PROPERTY_TYPE]->(PropertyType)
  (AuctionProperty)-[:HAS_BORROWER]->(Borrower)
  (AuctionProperty)-[:IS_AUCTION_TYPE]->(AuctionType)

Run:  python -m scripts.load_tn_to_neo4j
"""

import json
import os
import time
from neo4j import GraphDatabase

from pipeline.config import (
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE, LOOKUPS_DIR,
)

PROJECT_ROOT   = os.path.join(os.path.dirname(__file__), '..')
INPUT_FILE     = os.path.join(PROJECT_ROOT, "data", "tn_auction_data.jsonl")
BATCH_SIZE     = 100  # records per transaction

# Card-display abbreviation for long legal entity names (e.g. "SMFG INDIA
# CREDIT COMPANY LIMITED" -> "SMFG India Credit"), curated by hand per bank
# in bank_names.json. Falls back to the full name for any bank not yet
# curated, so every Bank node always gets a non-null short_name.
_BANK_SHORT_NAMES = json.loads(
    (LOOKUPS_DIR / "bank_names.json").read_text(encoding="utf-8")
).get("short_names", {})


def bank_short_name(name: str | None) -> str | None:
    if not name:
        return name
    if name in _BANK_SHORT_NAMES:
        return _BANK_SHORT_NAMES[name]
    lower_map = {k.lower(): v for k, v in _BANK_SHORT_NAMES.items()}
    return lower_map.get(name.lower(), name)

# ── Constraint / Index creation ───────────────────────────────────────────────
CONSTRAINTS = [
    "CREATE CONSTRAINT auction_id IF NOT EXISTS FOR (n:AuctionProperty) REQUIRE n.auction_id IS UNIQUE",
    "CREATE CONSTRAINT bank_name IF NOT EXISTS FOR (n:Bank) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT city_name IF NOT EXISTS FOR (n:City) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT state_name IF NOT EXISTS FOR (n:State) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT area_name IF NOT EXISTS FOR (n:Area) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT asset_cat IF NOT EXISTS FOR (n:AssetCategory) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT prop_type IF NOT EXISTS FOR (n:PropertyType) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT auction_type IF NOT EXISTS FOR (n:AuctionType) REQUIRE n.name IS UNIQUE",
    # Lucene fulltext index backing semantic_search's lexical "keyword" lens
    # (api/tools/cypher_tools.py: PROPERTY_FULLTEXT_INDEX).
    "CREATE FULLTEXT INDEX property_text_idx IF NOT EXISTS "
    "FOR (n:AuctionProperty) ON EACH [n.title, n.description]",
]

# ── Batch Cypher: create all nodes + relationships for a batch of records ─────
BATCH_QUERY = """
UNWIND $rows AS r

// ── AuctionProperty ───────────────────────────────────────────────────────
MERGE (a:AuctionProperty {auction_id: r.auction_id})
SET
  a.url                      = r.url,
  a.title                    = r.title,
  a.description              = r.description,
  a.website_description      = r.description,
  a.reserve_price_raw        = r.reserve_price_raw,
  a.reserve_price_num        = r.reserve_price_num,
  a.emd_raw                  = r.emd_raw,
  a.emd_num                  = r.emd_num,
  a.auction_start_dt         = CASE WHEN r.auction_start_dt        IS NULL THEN NULL ELSE datetime(r.auction_start_dt)        END,
  a.auction_end_dt           = CASE WHEN r.auction_end_dt          IS NULL THEN NULL ELSE datetime(r.auction_end_dt)          END,
  a.application_deadline_dt  = CASE WHEN r.application_deadline_dt IS NULL THEN NULL ELSE datetime(r.application_deadline_dt) END,
  a.downloads_list           = r.downloads_list,
  a.downloads_complete       = r.downloads_complete,
  a.contact_details          = r.contact_details,
  a.service_provider         = r.service_provider

// ── Bank ─────────────────────────────────────────────────────────────────
WITH a, r
WHERE r.bank_name IS NOT NULL AND r.bank_name <> ''
MERGE (b:Bank {name: r.bank_name})
SET b.short_name = r.bank_short_name
MERGE (a)-[:CONDUCTED_BY]->(b)

// ── Branch ────────────────────────────────────────────────────────────────
WITH a, b, r
WHERE r.branch_name IS NOT NULL AND r.branch_name <> ''
MERGE (br:Branch {name: r.branch_name})
MERGE (b)-[:HAS_BRANCH]->(br)
MERGE (a)-[:LISTED_BY_BRANCH]->(br)

// ── State ─────────────────────────────────────────────────────────────────
WITH a, r
WHERE r.state IS NOT NULL AND r.state <> ''
MERGE (st:State {name: r.state})
MERGE (a)-[:LOCATED_IN_STATE]->(st)

// ── City ──────────────────────────────────────────────────────────────────
WITH a, st, r
WHERE r.city IS NOT NULL AND r.city <> ''
MERGE (ci:City {name: r.city})
MERGE (ci)-[:IN_STATE]->(st)
MERGE (a)-[:LOCATED_IN_CITY]->(ci)

// ── Area ──────────────────────────────────────────────────────────────────
WITH a, ci, r
WHERE r.area IS NOT NULL AND r.area <> ''
MERGE (ar:Area {name: r.area})
MERGE (ar)-[:PART_OF_CITY]->(ci)
MERGE (a)-[:LOCATED_IN_AREA]->(ar)

// ── AssetCategory ─────────────────────────────────────────────────────────
WITH a, r
WHERE r.asset_category IS NOT NULL AND r.asset_category <> ''
MERGE (ac:AssetCategory {name: r.asset_category})
MERGE (a)-[:HAS_ASSET_CATEGORY]->(ac)

// ── PropertyType ──────────────────────────────────────────────────────────
// Auctions can have multiple property types (comma-separated in source).
// Link each directly from the auction, NOT through AssetCategory — the
// AssetCategory node is shared across auctions, so routing PropertyType
// through it leaked types across unrelated auctions.
WITH a, r
UNWIND coalesce(r.property_types, []) AS pt_name
WITH a, r, pt_name
WHERE pt_name IS NOT NULL AND pt_name <> ''
MERGE (pt:PropertyType {name: pt_name})
MERGE (a)-[:HAS_PROPERTY_TYPE]->(pt)

// ── AuctionType ───────────────────────────────────────────────────────────
WITH a, r
WHERE r.auction_type IS NOT NULL AND r.auction_type <> ''
MERGE (at:AuctionType {name: r.auction_type})
MERGE (a)-[:IS_AUCTION_TYPE]->(at)

// ── Borrower ──────────────────────────────────────────────────────────────
WITH a, r
WHERE r.borrower_name IS NOT NULL AND r.borrower_name <> ''
MERGE (bw:Borrower {name: r.borrower_name})
MERGE (a)-[:HAS_BORROWER]->(bw)
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def create_constraints(session):
    print("Creating constraints / indexes ...")
    for stmt in CONSTRAINTS:
        try:
            session.run(stmt)
        except Exception as e:
            print(f"  [WARN] {e}")
    print("  Done.")


def load_records(path: str) -> list[dict]:
    records = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def sanitise(r: dict) -> dict:
    """Ensure all fields Neo4j needs are present and None-safe."""
    def _str(v):  return str(v).strip() if v else None
    def _float(v): return float(v) if v is not None else None

    return {
        "auction_id"               : _str(r.get("auction_id")),
        "url"                      : _str(r.get("url")),
        "title"                    : _str(r.get("title")),
        "description"              : _str(r.get("description")),
        "reserve_price_raw"        : _str(r.get("reserve_price_raw")),
        "reserve_price_num"        : _float(r.get("reserve_price_num")),
        "emd_raw"                  : _str(r.get("emd_raw")),
        "emd_num"                  : _float(r.get("emd_num")),
        "auction_start_dt"         : _str(r.get("auction_start_dt")),
        "auction_end_dt"           : _str(r.get("auction_end_dt")),
        "application_deadline_dt"  : _str(r.get("application_deadline_dt")),
        "downloads_list"           : r.get("downloads_list") or [],
        "downloads_complete"       : bool(r.get("downloads_complete")),
        "contact_details"          : _str(r.get("contact_details")),
        "service_provider"         : _str(r.get("service_provider")),
        "bank_name"                : _str(r.get("bank_name")),
        "bank_short_name"          : bank_short_name(_str(r.get("bank_name"))),
        "branch_name"              : _str(r.get("branch_name")),
        "state"                    : _str(r.get("state")),
        "city"                     : _str(r.get("city")),
        "area"                     : _str(r.get("area")),
        "asset_category"           : _str(r.get("asset_category")),
        "property_types"           : [
            p for p in (r.get("property_types") or []) if p and str(p).strip()
        ],
        "auction_type"             : _str(r.get("auction_type")),
        "borrower_name"            : _str(r.get("borrower_name")),
    }


def run_batch(session, batch: list[dict]) -> int:
    session.run(BATCH_QUERY, rows=batch)
    return len(batch)


def get_existing_ids(session) -> set[str]:
    print("Fetching existing auction IDs (with complete downloads) from Neo4j...")
    result = session.run("MATCH (a:AuctionProperty) WHERE a.downloads_complete = true RETURN a.auction_id AS aid")
    return {record["aid"] for record in result if record["aid"]}

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"Connecting to {NEO4J_URI} ...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))

    with driver.session(database=NEO4J_DATABASE) as session:
        create_constraints(session)
        existing_ids = get_existing_ids(session)

    print(f"\nLoading records from {INPUT_FILE} ...")
    records = load_records(INPUT_FILE)
    all_rows = [sanitise(r) for r in records if r.get('downloads_found')]
    
    # Filter for brand new ones only
    rows = [r for r in all_rows if r.get('auction_id') not in existing_ids]
    total = len(rows)
    print(f"  {len(all_rows):,} valid records found in file.")
    print(f"  {total:,} NEW records to ingest (batch size: {BATCH_SIZE})")
    
    if total == 0:
        print("\nNo new records to ingest. Done.")
        driver.close()
        return

    ingested = 0
    errors   = 0
    t_start  = time.time()

    with driver.session(database=NEO4J_DATABASE) as session:
        for i in range(0, total, BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            try:
                run_batch(session, batch)
                ingested += len(batch)
                pct = ingested / total * 100
                elapsed = time.time() - t_start
                rate = ingested / elapsed if elapsed > 0 else 0
                eta  = (total - ingested) / rate if rate > 0 else 0
                print(f"  [{ingested:>5}/{total}] {pct:5.1f}%  |  {rate:.0f} rec/s  |  ETA {eta:.0f}s   ", end='\r')
            except Exception as e:
                errors += 1
                print(f"\n  [ERROR] batch {i}–{i+len(batch)}: {e}")

    elapsed = time.time() - t_start
    driver.close()
    print(f"\n\n{'='*50}")
    print(f"  Ingested : {ingested:,} / {total:,} records")
    print(f"  Errors   : {errors}")
    print(f"  Time     : {elapsed:.1f}s")
    print(f"{'='*50}")
    print(f"\nVerify in Neo4j Browser:")
    print("  MATCH (n) RETURN labels(n), count(n) ORDER BY count(n) DESC")


if __name__ == "__main__":
    main()
