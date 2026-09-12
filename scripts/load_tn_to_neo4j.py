"""
load_tn_to_neo4j.py
--------------------
Loads normalized listings into Neo4j Aura (cc513ea9).

Input: every ``data/listings/<source>.jsonl`` the harvest wrote (one file per
portal — eauctionsindia, baanknet, bankeauctions), or the legacy
``data/tn_auction_data.jsonl`` when no listings directory exists yet. Rows
from any source share one shape (``sources.base.Listing.to_row``); a legacy
row without a ``source`` is eauctionsindia.

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
  (AuctionProperty)-[:HAS_MEDIA]->(Media)          portal photos / videos

plus, on the listing, the source properties from docs/SCHEMA.md "Sources and
the spine": ``source``, ``source_id``, ``source_url``, ``source_rank``,
``fetched_at``, ``last_seen_at``, and the portal-supplied ``portal_district``,
``pincode``, ``borrower_address``, ``possession_type``, ``extent_raw``,
``bid_increment_num``, ``inspection_*``, ``auction_status``, ``photo_urls``.

Run:  python -m scripts.load_tn_to_neo4j                      # data/listings/*.jsonl
      python -m scripts.load_tn_to_neo4j --input data/listings/baanknet.jsonl --dry-run
      python -m scripts.load_tn_to_neo4j --backfill-source     # once: stamp old nodes eauctionsindia
"""

import argparse
import glob
import json
import os
import sys
import time
from pathlib import Path

from neo4j import GraphDatabase

from pipeline.config import (
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE, LOOKUPS_DIR,
)
from sources.base import SOURCE_RANK

PROJECT_ROOT   = os.path.join(os.path.dirname(__file__), '..')
INPUT_FILE     = os.path.join(PROJECT_ROOT, "data", "tn_auction_data.jsonl")
LISTINGS_GLOB  = os.path.join(PROJECT_ROOT, "data", "listings", "*.jsonl")
BATCH_SIZE     = 100  # records per transaction
DEFAULT_SOURCE = "eauctionsindia"

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
    # One node per photo/video URL; the batch below MERGEs on it.
    "CREATE CONSTRAINT media_url_unique IF NOT EXISTS FOR (n:Media) REQUIRE n.url IS UNIQUE",
    # One notice published against N lots is stored as N Documents holding the
    # same bytes; pipeline/notice_twins groups on this hash so the paid passes
    # (OCR, extraction) run once per page. Looked up by file, so it needs to be
    # a point lookup, not a scan.
    "CREATE INDEX document_content_sha IF NOT EXISTS "
    "FOR (n:Document) ON (n.content_sha256)",
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
  // Money is the one field a human corrects against the notice, and an
  // unconditional SET here undoes that on the next load — silently, because
  // the loader has no idea a correction happened. `price_corrected_at` marks a
  // listing whose figure was read off the sale notice after the portal's
  // scrape got it wrong: 766811 lost a leading digit from BOTH price and EMD
  // ("Rs.11,19,600/-" scraped as ₹1,19,600), 795611 by a factor of ten against
  // a notice that spells the amount out in words. The portal's own value is
  // never lost — the correction stashes it in `portal_reserve_price_*` /
  // `portal_emd_*` — so this skip costs nothing and re-running the loader
  // stops being destructive.
  //
  // Only these four are guarded. Everything else on the node is the portal's
  // to state, and a correction workflow for those does not exist yet.
  a.reserve_price_raw        = CASE WHEN a.price_corrected_at IS NULL
                                    THEN r.reserve_price_raw
                                    ELSE a.reserve_price_raw END,
  a.reserve_price_num        = CASE WHEN a.price_corrected_at IS NULL
                                    THEN r.reserve_price_num
                                    ELSE a.reserve_price_num END,
  a.emd_raw                  = CASE WHEN a.price_corrected_at IS NULL
                                    THEN r.emd_raw ELSE a.emd_raw END,
  a.emd_num                  = CASE WHEN a.price_corrected_at IS NULL
                                    THEN r.emd_num ELSE a.emd_num END,
  // The portal's latest figure is still recorded, so a scrape that FIXES its
  // own error is visible rather than swallowed: when these two agree again,
  // the correction has served its purpose and can be retired.
  a.portal_reserve_price_num = CASE WHEN a.price_corrected_at IS NULL
                                    THEN a.portal_reserve_price_num
                                    ELSE r.reserve_price_num END,
  a.portal_emd_num           = CASE WHEN a.price_corrected_at IS NULL
                                    THEN a.portal_emd_num
                                    ELSE r.emd_num END,
  a.auction_start_dt         = CASE WHEN r.auction_start_dt        IS NULL THEN NULL ELSE datetime(r.auction_start_dt)        END,
  a.auction_end_dt           = CASE WHEN r.auction_end_dt          IS NULL THEN NULL ELSE datetime(r.auction_end_dt)          END,
  a.application_deadline_dt  = CASE WHEN r.application_deadline_dt IS NULL THEN NULL ELSE datetime(r.application_deadline_dt) END,
  a.downloads_list           = r.downloads_list,
  a.downloads_complete       = r.downloads_complete,
  a.contact_details          = r.contact_details,
  a.service_provider         = r.service_provider,
  // ── source (docs/SCHEMA.md "Sources and the spine") ─────────────────────
  a.source                   = r.source,
  a.source_id                = r.source_id,
  a.source_url               = r.source_url,
  a.source_rank              = r.source_rank,
  a.fetched_at               = CASE WHEN r.fetched_at IS NULL THEN a.fetched_at ELSE datetime(r.fetched_at) END,
  a.last_seen_at             = datetime(),
  a.portal_district          = r.portal_district,
  a.pincode                  = r.pincode,
  a.borrower_address         = r.borrower_address,
  a.possession_type          = r.possession_type,
  a.extent_raw               = r.extent_raw,
  a.bid_increment_num        = r.bid_increment_num,
  a.inspection_start_dt      = CASE WHEN r.inspection_start_dt IS NULL THEN NULL ELSE datetime(r.inspection_start_dt) END,
  a.inspection_end_dt        = CASE WHEN r.inspection_end_dt   IS NULL THEN NULL ELSE datetime(r.inspection_end_dt)   END,
  a.auction_status           = r.auction_status,
  // parallel to downloads_list: the adapter's role for each file, and where
  // it came from — what upload_downloads_to_r2 stamps on each :Document
  a.document_roles           = r.document_roles,
  a.document_urls            = r.document_urls,
  a.photo_urls               = r.photo_urls

// Every block below is guarded by its own FOREACH rather than a shared
// `WITH a, r WHERE ...`. That chained form filtered the ROW, not just the
// block, so a single empty field silently suppressed every block after it —
// an empty auction_type cost 646 properties their Borrower edge too, and an
// empty property_types list did the same via `UNWIND []` yielding no rows.
// FOREACH over a 0-or-1 element list skips only its own body.

// ── Bank (+ Branch, which needs the Bank node) ────────────────────────────
FOREACH (_ IN CASE WHEN coalesce(r.bank_name, '') <> '' THEN [1] ELSE [] END |
  MERGE (b:Bank {name: r.bank_name})
  SET b.short_name = r.bank_short_name
  MERGE (a)-[:CONDUCTED_BY]->(b)
  FOREACH (__ IN CASE WHEN coalesce(r.branch_name, '') <> '' THEN [1] ELSE [] END |
    MERGE (br:Branch {name: r.branch_name})
    MERGE (b)-[:HAS_BRANCH]->(br)
    MERGE (a)-[:LISTED_BY_BRANCH]->(br)
  )
)

// ── State → City → Area (each nested inside its parent) ───────────────────
FOREACH (_ IN CASE WHEN coalesce(r.state, '') <> '' THEN [1] ELSE [] END |
  MERGE (st:State {name: r.state})
  MERGE (a)-[:LOCATED_IN_STATE]->(st)
  FOREACH (__ IN CASE WHEN coalesce(r.city, '') <> '' THEN [1] ELSE [] END |
    MERGE (ci:City {name: r.city})
    MERGE (ci)-[:IN_STATE]->(st)
    MERGE (a)-[:LOCATED_IN_CITY]->(ci)
    FOREACH (___ IN CASE WHEN coalesce(r.area, '') <> '' THEN [1] ELSE [] END |
      MERGE (ar:Area {name: r.area})
      MERGE (ar)-[:PART_OF_CITY]->(ci)
      MERGE (a)-[:LOCATED_IN_AREA]->(ar)
    )
  )
)

// ── AssetCategory ─────────────────────────────────────────────────────────
FOREACH (_ IN CASE WHEN coalesce(r.asset_category, '') <> '' THEN [1] ELSE [] END |
  MERGE (ac:AssetCategory {name: r.asset_category})
  MERGE (a)-[:HAS_ASSET_CATEGORY]->(ac)
)

// ── PropertyType ──────────────────────────────────────────────────────────
// Auctions can have multiple property types (comma-separated in source).
// Link each directly from the auction, NOT through AssetCategory — the
// AssetCategory node is shared across auctions, so routing PropertyType
// through it leaked types across unrelated auctions.
// FOREACH over the list, not UNWIND: an empty list must skip this block only.
FOREACH (pt_name IN [x IN coalesce(r.property_types, [])
                     WHERE x IS NOT NULL AND x <> ''] |
  MERGE (pt:PropertyType {name: pt_name})
  MERGE (a)-[:HAS_PROPERTY_TYPE]->(pt)
)

// ── AuctionType ───────────────────────────────────────────────────────────
FOREACH (_ IN CASE WHEN coalesce(r.auction_type, '') <> '' THEN [1] ELSE [] END |
  MERGE (at:AuctionType {name: r.auction_type})
  MERGE (a)-[:IS_AUCTION_TYPE]->(at)
)

// ── Borrower ──────────────────────────────────────────────────────────────
FOREACH (_ IN CASE WHEN coalesce(r.borrower_name, '') <> '' THEN [1] ELSE [] END |
  MERGE (bw:Borrower {name: r.borrower_name})
  MERGE (a)-[:HAS_BORROWER]->(bw)
)

// ── Media ─────────────────────────────────────────────────────────────────
// One node per URL. Videos stay links; upload_downloads_to_r2 mirrors the
// photos of live listings and fills content_sha256 / r2_key / public_url.
FOREACH (m IN [x IN coalesce(r.media, []) WHERE coalesce(x.url, '') <> ''] |
  MERGE (md:Media {url: m.url})
  SET md.kind    = m.kind,
      md.is_main = m.is_main,
      md.label   = m.label,
      md.source  = r.source
  MERGE (a)-[:HAS_MEDIA]->(md)
)
"""

# One-off, idempotent: every listing loaded before the adapters existed came
# from eauctionsindia. The batch above never re-SETs a complete listing, so
# these nodes need stamping once.
BACKFILL_SOURCE_QUERY = """
MATCH (a:AuctionProperty) WHERE a.source IS NULL
SET a.source = $source, a.source_rank = $rank,
    a.source_id = coalesce(a.source_id, a.auction_id),
    a.source_url = coalesce(a.source_url, a.url)
RETURN count(a) AS n
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
    """Ensure all fields Neo4j needs are present and None-safe.

    A row without ``source`` is a legacy ``tn_auction_data.jsonl`` row:
    eauctionsindia, rank 3, its own id and url as source id and url.
    """
    def _str(v):  return str(v).strip() if v else None
    def _float(v): return float(v) if v is not None else None

    source = _str(r.get("source")) or DEFAULT_SOURCE
    documents = [d for d in (r.get("documents") or []) if isinstance(d, dict)]
    media = [{"url": _str(m.get("url")), "kind": _str(m.get("kind")) or "image",
              "is_main": bool(m.get("is_main")), "label": _str(m.get("label"))}
             for m in (r.get("media") or []) if isinstance(m, dict) and _str(m.get("url"))]
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
        # ── source ──
        "source"                   : source,
        "source_id"                : _str(r.get("source_id")) or _str(r.get("auction_id")),
        "source_url"               : _str(r.get("source_url")) or _str(r.get("url")),
        "source_rank"              : int(r.get("source_rank") or SOURCE_RANK.get(source, 9)),
        "fetched_at"               : _str(r.get("fetched_at")),
        "portal_district"          : _str(r.get("district")),
        "pincode"                  : _str(r.get("pincode")),
        "borrower_address"         : _str(r.get("borrower_address")),
        "possession_type"          : _str(r.get("possession_type")),
        "extent_raw"               : _str(r.get("extent_raw")),
        "bid_increment_num"        : _float(r.get("bid_increment_num")),
        "inspection_start_dt"      : _str(r.get("inspection_start_dt")),
        "inspection_end_dt"        : _str(r.get("inspection_end_dt")),
        "auction_status"           : _str(r.get("auction_status")),
        "document_roles"           : [_str(d.get("doc_role")) or "unknown" for d in documents],
        "document_urls"            : [_str(d.get("url")) or "" for d in documents],
        "media"                    : media,
        "photo_urls"               : [m["url"] for m in media if m["kind"] == "image"],
    }


def is_loadable(r: dict) -> bool:
    """A listing is worth a node when it brought at least one document or one
    photo. (Before the adapters the rule was "≥1 download found".)"""
    return bool(r.get("downloads_found")) or any(
        isinstance(m, dict) and m.get("url") for m in (r.get("media") or []))


def resolve_inputs(inputs: list[str] | None) -> list[Path]:
    """The files to load: the ``--input`` globs, else every
    ``data/listings/*.jsonl``, else the legacy ``tn_auction_data.jsonl``."""
    patterns = inputs or [LISTINGS_GLOB]
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(Path(p) for p in sorted(glob.glob(pattern)))
    if not paths and not inputs:
        paths = [Path(INPUT_FILE)]
    return [p for p in paths if p.is_file()]


def load_inputs(paths: list[Path]) -> list[dict]:
    """Rows from every file, later files winning on a repeated auction_id
    (the same eauctionsindia listing can sit in both the legacy file and
    ``data/listings/eauctionsindia.jsonl``)."""
    by_id: dict[str, dict] = {}
    for path in paths:
        for r in load_records(str(path)):
            aid = str(r.get("auction_id") or "").strip()
            if aid:
                by_id[aid] = r
    return list(by_id.values())


def run_batch(session, batch: list[dict]) -> int:
    session.run(BATCH_QUERY, rows=batch)
    return len(batch)


def get_existing_ids(session) -> set[str]:
    print("Fetching existing auction IDs (with complete downloads) from Neo4j...")
    result = session.run("MATCH (a:AuctionProperty) WHERE a.downloads_complete = true RETURN a.auction_id AS aid")
    return {record["aid"] for record in result if record["aid"]}

# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", action="append", default=None,
                    help="listing file or glob (repeatable); default data/listings/*.jsonl, else data/tn_auction_data.jsonl")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and filter, print the first batch's rows; no connection to Neo4j")
    ap.add_argument("--backfill-source", action="store_true",
                    help="stamp source='eauctionsindia' on listings loaded before the adapters existed (idempotent)")
    args = ap.parse_args(argv)

    paths = resolve_inputs(args.input)
    if not paths:
        print(f"no input files ({args.input or [LISTINGS_GLOB, INPUT_FILE]})", file=sys.stderr)
        return 1
    print("Loading records from " + ", ".join(str(p) for p in paths) + " ...")
    records = load_inputs(paths)
    all_rows = [sanitise(r) for r in records if is_loadable(r)]
    by_source: dict[str, int] = {}
    for r in all_rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    print(f"  {len(records):,} records in file(s); {len(all_rows):,} with a document or photo "
          f"({', '.join(f'{k} {v}' for k, v in sorted(by_source.items()))})")

    if args.dry_run:
        print(f"\n[dry-run] first batch ({min(BATCH_SIZE, len(all_rows))} rows); nothing sent")
        for r in all_rows[:3]:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    print(f"Connecting to {NEO4J_URI} ...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))

    with driver.session(database=NEO4J_DATABASE) as session:
        create_constraints(session)
        if args.backfill_source:
            n = session.run(BACKFILL_SOURCE_QUERY, source=DEFAULT_SOURCE,
                            rank=SOURCE_RANK[DEFAULT_SOURCE]).single()["n"]
            print(f"  backfilled source={DEFAULT_SOURCE} on {n:,} listings")
        existing_ids = get_existing_ids(session)

    # Filter for brand new ones only
    rows = [r for r in all_rows if r.get('auction_id') not in existing_ids]
    total = len(rows)
    print(f"  {total:,} NEW records to ingest (batch size: {BATCH_SIZE})")

    if total == 0:
        print("\nNo new records to ingest. Done.")
        driver.close()
        return 0

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
    print("\nVerify in Neo4j Browser:")
    print("  MATCH (a:AuctionProperty) RETURN a.source, count(*)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
