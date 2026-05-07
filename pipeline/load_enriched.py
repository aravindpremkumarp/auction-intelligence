"""
pipeline/load_enriched.py
-------------------------
Stage 4: Load enriched data into Neo4j (PRD 5.9).

Reads normalized.jsonl and extends existing AuctionProperty nodes with
new properties.

Run standalone:  python -m pipeline.load_enriched
"""

import json
import time
from datetime import datetime, timezone
from neo4j import GraphDatabase

from pipeline.config import (
    OUTPUT_DIR, NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD,
    NEO4J_DATABASE, NEO4J_BATCH_SIZE,
)

NORMALIZED_JSONL = OUTPUT_DIR / "normalized.jsonl"
VERIFIED_JSONL   = OUTPUT_DIR / "verified_enriched.jsonl"

# AuctionProperty date properties stored as Neo4j DATETIME. The driver
# auto-coerces Python datetime to Neo4j DATETIME, but verified_fields and
# scraped_originals flow into Cypher via SET a += map — there's no spot
# for a per-key datetime() cast. Convert ISO strings to datetime here.
_DATE_KEYS = {
    "auction_start_dt", "auction_end_dt", "application_deadline_dt",
    "auction_start_dt_scraped", "auction_end_dt_scraped",
    "application_deadline_dt_scraped",
}


def _coerce_date_keys(d: dict | None) -> dict | None:
    if not d:
        return d
    out = dict(d)
    for k in _DATE_KEYS:
        v = out.get(k)
        if isinstance(v, str) and v:
            out[k] = datetime.fromisoformat(v)
    return out

# The legacy ``doc_path`` uniqueness constraint is intentionally dropped:
# ``file_path`` carried mixed values (absolute filesystem path vs bare
# filename vs R2 storage_key) which produced duplicate :Document nodes
# under the same property (issue #45). The stable identity is now
# (auction_id, filename) enforced via the relationship-anchored MERGE in
# VERIFIED_DOC_QUERY below.
DROP_CONSTRAINTS = [
    "DROP CONSTRAINT doc_path IF EXISTS",
]
NEW_CONSTRAINTS = [
    "CREATE CONSTRAINT doc_storage_key IF NOT EXISTS FOR (n:Document) REQUIRE n.storage_key IS UNIQUE",
]

# ── Verified + enriched upsert (reads verified_enriched.jsonl) ───────────────
VERIFIED_UPSERT_QUERY = """
UNWIND $rows AS r

MATCH (a:AuctionProperty {auction_id: r.auction_id})
SET a += r.verified_fields          // PDF values overwrite scraped scalars
SET a += r.enrichment_flat          // flat enrichment props (boundary_*, undivided_share, ...)
SET a += r.scraped_originals        // *_scraped mirrors for audit
SET a.extras_json           = r.extras_json,
    a.enriched_description  = r.enriched_description,
    a.field_conflicts       = r.field_conflicts,
    a.verification_status   = r.verification_status,
    a.verified_at           = datetime()
"""

VERIFIED_DOC_QUERY = """
UNWIND $rows AS r
MATCH (a:AuctionProperty {auction_id: r.auction_id})
UNWIND r.documents AS d
MERGE (a)-[:HAS_DOCUMENT]->(doc:Document {filename: d.filename})
SET doc.file_path      = d.file_path,
    doc.doc_type       = d.doc_type,
    doc.extracted_json = d.extracted_fields_json,
    doc.extracted_at   = d.extracted_at,
    doc.model          = d.model
"""

# ── Batch Cypher: update AuctionProperty ─────────────────────────────────────
ENRICHMENT_QUERY = """
UNWIND $rows AS r

// ── Match existing AuctionProperty ────────────────────────────────────────
MATCH (a:AuctionProperty {auction_id: r.auction_id})

// ── Set enriched properties (only if non-null) ───────────────────────────
SET
  a.undivided_share             = COALESCE(r.undivided_share, a.undivided_share),
  a.total_area                  = COALESCE(r.total_area, a.total_area),
  a.village                     = COALESCE(r.village, a.village),
  a.taluk                       = COALESCE(r.taluk, a.taluk),
  a.district                    = COALESCE(r.district, a.district),
  a.registration_district       = COALESCE(r.registration_district, a.registration_district),
  a.registration_sub_district   = COALESCE(r.registration_sub_district, a.registration_sub_district),
  a.boundary_north              = COALESCE(r.boundary_north, a.boundary_north),
  a.boundary_south              = COALESCE(r.boundary_south, a.boundary_south),
  a.boundary_east               = COALESCE(r.boundary_east, a.boundary_east),
  a.boundary_west               = COALESCE(r.boundary_west, a.boundary_west),
  a.door_numbers_old            = COALESCE(r.door_numbers_old, a.door_numbers_old),
  a.door_numbers_new            = COALESCE(r.door_numbers_new, a.door_numbers_new),
  a.extracted_description       = COALESCE(r.extracted_description, a.extracted_description),
  a.description_completeness    = COALESCE(r.description_completeness, a.description_completeness),
  a.extraction_date             = r.extraction_date
"""

def load_records() -> list[dict]:
    """Load normalized records."""
    records = []
    with open(NORMALIZED_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def prepare_row(record: dict) -> dict:
    """Prepare a record for the Cypher query."""
    enriched = record.get("enriched_fields", {})
    now = datetime.now(timezone.utc).isoformat()

    return {
        "auction_id": record["auction_id"],
        "undivided_share": enriched.get("undivided_share"),
        "total_area": enriched.get("total_area"),
        "village": enriched.get("village"),
        "taluk": enriched.get("taluk"),
        "district": enriched.get("district"),
        "registration_district": enriched.get("registration_district"),
        "registration_sub_district": enriched.get("registration_sub_district"),
        "boundary_north": enriched.get("boundary_north"),
        "boundary_south": enriched.get("boundary_south"),
        "boundary_east": enriched.get("boundary_east"),
        "boundary_west": enriched.get("boundary_west"),
        "door_numbers_old": enriched.get("door_numbers_old"),
        "door_numbers_new": enriched.get("door_numbers_new"),
        "extracted_description": enriched.get("property_description_full"),
        "description_completeness": record.get("cross_reference", {}).get("description_completeness"),
        "extraction_date": now,
    }


def create_constraints(session):
    """Create new constraints for enriched data."""
    print("Dropping legacy constraints (issue #45)...")
    for stmt in DROP_CONSTRAINTS:
        try:
            session.run(stmt)
            print(f"  OK: {stmt}")
        except Exception as e:
            print(f"  [WARN] {e}")

    print("Creating new constraints...")
    for stmt in NEW_CONSTRAINTS:
        try:
            session.run(stmt)
            print(f"  OK: {stmt[:80]}...")
        except Exception as e:
            print(f"  [WARN] {e}")


def load_to_neo4j():
    """Load enriched data into Neo4j."""
    if not NORMALIZED_JSONL.exists():
        print("No normalized.jsonl found. Run normalize first.")
        return

    records = load_records()
    total = len(records)
    print(f"Loading {total} enriched records into Neo4j...")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))

    # Create constraints
    with driver.session(database=NEO4J_DATABASE) as session:
        create_constraints(session)

    # Prepare data
    rows = [prepare_row(r) for r in records]

    # Load enriched properties in batches
    ingested = 0
    errors = 0
    t_start = time.time()

    with driver.session(database=NEO4J_DATABASE) as session:
        for i in range(0, len(rows), NEO4J_BATCH_SIZE):
            batch = rows[i : i + NEO4J_BATCH_SIZE]
            try:
                session.run(ENRICHMENT_QUERY, rows=batch)
                ingested += len(batch)
                elapsed = time.time() - t_start
                rate = ingested / elapsed if elapsed > 0 else 0
                print(f"  Properties: [{ingested}/{total}] {rate:.0f} rec/s", end="\r")
            except Exception as e:
                errors += 1
                print(f"\n  [ERROR] batch {i}: {e}")

    driver.close()

    elapsed = time.time() - t_start
    print(f"\n\n{'='*50}")
    print(f"  Properties updated : {ingested}")
    print(f"  Errors             : {errors}")
    print(f"  Time               : {elapsed:.1f}s")
    print(f"{'='*50}")
    print("\nVerify:")
    print("  MATCH (a:AuctionProperty) WHERE a.undivided_share IS NOT NULL RETURN count(a)")


def load_verified_enriched() -> None:
    """Load pipeline/output/verified_enriched.jsonl into Neo4j.

    Upserts verified fields, scraped-original mirrors, flat enrichment props,
    extras JSON, and :Document nodes linked via [:HAS_DOCUMENT]."""
    if not VERIFIED_JSONL.exists():
        print(f"No {VERIFIED_JSONL.name} found. Run verify_and_enrich first.")
        return

    rows: list[dict] = []
    with open(VERIFIED_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    for r in rows:
        r["verified_fields"]   = _coerce_date_keys(r.get("verified_fields"))
        r["scraped_originals"] = _coerce_date_keys(r.get("scraped_originals"))

    total = len(rows)
    print(f"Loading {total} verified+enriched records into Neo4j...")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))

    with driver.session(database=NEO4J_DATABASE) as session:
        create_constraints(session)

    ingested = 0
    doc_batches = 0
    errors = 0
    t_start = time.time()

    with driver.session(database=NEO4J_DATABASE) as session:
        for i in range(0, total, NEO4J_BATCH_SIZE):
            batch = rows[i : i + NEO4J_BATCH_SIZE]
            try:
                session.run(VERIFIED_UPSERT_QUERY, rows=batch)
                doc_batch = [r for r in batch if r.get("documents")]
                if doc_batch:
                    session.run(VERIFIED_DOC_QUERY, rows=doc_batch)
                    doc_batches += 1
                ingested += len(batch)
                elapsed = time.time() - t_start
                rate = ingested / elapsed if elapsed > 0 else 0
                print(f"  Verified: [{ingested}/{total}] {rate:.0f} rec/s", end="\r")
            except Exception as e:
                errors += 1
                print(f"\n  [ERROR] batch {i}: {e}")

    driver.close()
    elapsed = time.time() - t_start
    print(f"\n\n{'='*50}")
    print(f"  Properties upserted : {ingested}")
    print(f"  Doc batches         : {doc_batches}")
    print(f"  Errors              : {errors}")
    print(f"  Time                : {elapsed:.1f}s")
    print(f"{'='*50}")
    print("\nVerify:")
    print("  MATCH (a:AuctionProperty) WHERE size(a.field_conflicts) > 0 RETURN count(a)")
    print("  MATCH (a)-[:HAS_DOCUMENT]->(d:Document) RETURN count(d)")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--verified", action="store_true",
                        help="Load from verified_enriched.jsonl instead of normalized.jsonl")
    args = parser.parse_args()
    if args.verified:
        load_verified_enriched()
    else:
        load_to_neo4j()
