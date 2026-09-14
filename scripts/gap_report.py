"""
gap_report.py — what the new portals would add, before anything writes to the graph.

Reads the harvested rows in data/listings/<source>.jsonl, fetches what the
graph already holds (read-only), runs the cross-portal matcher and prints,
per portal:

  rows            harvested listings
  already loaded  same auction_id is in the graph (a re-run of a loaded source)
  new             no listing of another source agrees on reserve price or borrower
  review          a partial agreement waiting for a person, by reason — batch,
                  units_disagree, price_only, borrower_only, split, contested; a listing
                  waiting against two other sources counts once in review and under each of
                  its reasons
  confirmed       bank, auction day, reserve price and borrower agree on one listing
                  (four_fields), a unit number settled a batch (unit_number), or a
                  person confirmed it (decision)
  fills           for matched listings, which of the nine core fields the
                  portal has that the graph's listing lacks
  photos gained   listings with photos, split new / confirmed

Nothing here writes. It is the decision input for Task 8 (loader) and Task 9
(spine): if a portal adds nothing but duplicates, it is not worth loading.

Usage:
    python -m scripts.gap_report                             # every data/listings/*.jsonl
    python -m scripts.gap_report --source baanknet --json out.json
    python -m scripts.gap_report --existing-json graph.json  # offline: a saved fetch
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sources.match import (  # noqa: E402
    SIDES, Ambiguity, Candidate, Pair, candidate_from_graph, candidate_from_row, extract_boundaries,
    extract_extent, extract_identifiers, match_listings,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DOWNLOADS_DIR = PROJECT_ROOT / "downloads"

#: The nine-field property core (docs/SCHEMA.md, "Sources and the spine").
CORE_FIELDS = ("property_type", "location", "extent", "measurement", "possession",
               "boundaries", "reserve_price", "auction_date", "has_photos")

# A boundary length: "37 feet", "19 ft", "12.5 mtrs". Road widths ("20 feet
# road") match too, so two hits are asked for before calling it a measurement.
_LENGTH = re.compile(r"\d+(?:\.\d+)?\s*(?:feet|ft|foot|mtrs?|metres?|meters?|m)\b\.?", re.IGNORECASE)
_NOT_STATED = {"", "not stated", "unknown", "none", "n/a"}

# ── the graph side ───────────────────────────────────────────────────────────

FETCH_EXISTING = """
MATCH (a:AuctionProperty)
OPTIONAL MATCH (a)-[:CONDUCTED_BY]->(bk:Bank)
OPTIONAL MATCH (a)-[:HAS_BORROWER]->(br:Borrower)
OPTIONAL MATCH (a)-[:LOCATED_IN_DISTRICT]->(d:District)
OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(c:City)
OPTIONAL MATCH (a)-[:HAS_PROPERTY_TYPE]->(pt:PropertyType)
WITH a, collect(DISTINCT bk.name)[0] AS bank, collect(DISTINCT d.name)[0] AS district_node,
     collect(DISTINCT c.name)[0] AS city, collect(DISTINCT br.name)[0] AS borrower, collect(DISTINCT pt.name) AS ptypes
CALL { WITH a OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(doc:Document)
       RETURN [s IN collect(DISTINCT doc.content_sha256) WHERE s IS NOT NULL] AS doc_shas, count(doc) AS n_docs,
              [u IN collect(DISTINCT doc.public_url) WHERE u IS NOT NULL][0] AS public_url }
CALL { WITH a OPTIONAL MATCH (a)-[:IS_LOT]->(l:Lot)
       OPTIONAL MATCH (l)-[:MENTIONS_IDENTIFIER]->(i:Identifier)
       OPTIONAL MATCH (l)-[:HAS_BOUNDARY]->(b:Boundary)
       OPTIONAL MATCH (l)-[:HAS_EXTENT]->(m:Measurement)
       OPTIONAL MATCH (l)-[:POSSESSION_IS]->(p:PossessionType)
       RETURN [x IN collect(DISTINCT [i.kind, i.value_norm]) WHERE x[0] IS NOT NULL] AS identifiers,
              [x IN collect(DISTINCT [b.side, b.adjacency_raw, b.measurement_ft]) WHERE x[0] IS NOT NULL] AS lot_bounds,
              count(DISTINCT m) AS n_extents,
              [x IN collect(DISTINCT coalesce(p.name, l.possession_stated)) WHERE x IS NOT NULL][0] AS possession }
RETURN a.auction_id AS auction_id, coalesce(a.source, 'eauctionsindia') AS source, bank,
       a.reserve_price_num AS reserve_price_num, toString(a.auction_start_dt) AS auction_start_dt,
       borrower, doc_shas, n_docs, identifiers, lot_bounds, n_extents, possession,
       coalesce(a.property_type_effective, a.property_type_norm, ptypes[0]) AS property_type,
       coalesce(a.revenue_district, a.district, district_node) AS district,
       city,
       coalesce(a.enriched_description, a.description) AS description,
       a.total_area AS total_area,
       {north: a.boundary_north, south: a.boundary_south, east: a.boundary_east, west: a.boundary_west} AS boundaries,
       [a.boundary_measurement_north, a.boundary_measurement_south,
        a.boundary_measurement_east, a.boundary_measurement_west] AS boundary_measurements,
       a.photo_urls AS photo_urls
       , a.title AS title, coalesce(a.source_url, a.url) AS url, a.emd_num AS emd_num, public_url
"""


def fetch_existing() -> list[dict]:
    """Every listing the graph holds, in the shape ``build_report`` reads.
    Read-only: runs inside ``execute_read``."""
    from neo4j import GraphDatabase

    from pipeline.config import NEO4J_DATABASE, NEO4J_PASSWORD, NEO4J_URI, NEO4J_USERNAME

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    try:
        with driver.session(database=NEO4J_DATABASE) as session:
            return session.execute_read(lambda tx: [dict(r) for r in tx.run(FETCH_EXISTING)])
    finally:
        driver.close()


def graph_boundaries(rec: dict) -> dict[str, str]:
    """The four sides as the graph knows them: the listing's own
    ``boundary_*`` first, then the lot's ``:Boundary`` nodes for any side
    still missing."""
    out = {s: v for s, v in (rec.get("boundaries") or {}).items() if v}
    for entry in rec.get("lot_bounds") or ():
        side, adjacency = entry[0], entry[1] if len(entry) > 1 else None
        if side and adjacency and side not in out:
            out[side] = adjacency
    return out


def core_from_graph(rec: dict) -> dict[str, bool]:
    """Which of the nine core fields the graph's listing already has.

    Structured first (lots, boundary and measurement nodes, resolved
    district), then the listing's own description text with the same
    detectors ``core_from_row`` uses — so a listing the pipeline has not
    read yet (no lots, no price, a city but no district: the shape of every
    recent eauctionsindia row) is credited with what its text states, and a
    "fill" means the portal states something the graph's listing states
    nowhere.
    """
    text = rec.get("description") or ""
    bounds = graph_boundaries(rec) or extract_boundaries(text)
    measured = any(v for v in rec.get("boundary_measurements") or ()) or any(
        len(e) > 2 and e[2] for e in rec.get("lot_bounds") or ()) or len(_LENGTH.findall(text)) >= 2
    possession = (rec.get("possession") or "").strip().lower()
    return {
        "property_type": bool(rec.get("property_type")),
        "location": bool(rec.get("district") or rec.get("city")),
        "extent": bool(rec.get("n_extents")) or bool(rec.get("total_area")) or bool(extract_extent(text)),
        "measurement": measured,
        "possession": possession not in _NOT_STATED,
        "boundaries": all(bounds.get(s) for s in SIDES),
        "reserve_price": bool(rec.get("reserve_price_num")),
        "auction_date": bool(rec.get("auction_start_dt")),
        "has_photos": bool(rec.get("photo_urls")),
    }


def graph_candidate(rec: dict) -> Candidate:
    """The matcher's view of a graph listing: lot-level boundaries where the
    pipeline has produced them, else whatever the listing's description
    states; identifiers from both the lot and the description, because lot
    extraction reads survey and door numbers but a batch sale is told apart
    by the villa or flat number only the description quotes."""
    text = rec.get("description") or ""
    cand = candidate_from_graph({**rec, "boundaries": graph_boundaries(rec) or extract_boundaries(text)})
    cand.identifiers |= extract_identifiers(text)
    return cand


# ── the portal side ──────────────────────────────────────────────────────────


def core_from_row(row: dict) -> dict[str, bool]:
    """Which of the nine core fields a harvested row carries. Text-derived
    ones (extent, measurement, boundaries) are read from title + description
    the way the matcher does; they say the portal *states* the fact, not that
    it has been extracted yet."""
    text = " ".join(t for t in (row.get("title"), row.get("description")) if t)
    return {
        "property_type": bool(row.get("property_types") or row.get("property_type_raw")),
        "location": bool(row.get("district") or row.get("city")),
        "extent": bool(row.get("extent_raw")) or bool(extract_extent(text)),
        "measurement": len(_LENGTH.findall(text)) >= 2,
        "possession": (row.get("possession_type") or "").strip().lower() not in _NOT_STATED,
        "boundaries": len(extract_boundaries(text)) == 4,
        "reserve_price": bool(row.get("reserve_price_num")),
        "auction_date": bool(row.get("auction_start_dt")),
        "has_photos": bool(row.get("has_photos")),
    }


def load_rows(data_dir: Path, sources: list[str] | None = None) -> dict[str, list[dict]]:
    """``{source: rows}`` from ``data/listings/<source>.jsonl``."""
    out: dict[str, list[dict]] = {}
    for path in sorted((data_dir / "listings").glob("*.jsonl")):
        name = path.stem
        if sources and name not in sources:
            continue
        with open(path, encoding="utf-8") as fh:
            out[name] = [json.loads(line) for line in fh if line.strip()]
    return out


def download_shas(row: dict, downloads_dir: Path) -> list[str]:
    """SHA-256 of each file the harvest found for this row — the same
    fingerprint ``:Document.content_sha256`` carries."""
    shas = []
    src_dir = downloads_dir / str(row.get("source") or "")
    for name in row.get("downloads_found") or ():
        path = src_dir / name
        if path.is_file():
            shas.append(hashlib.sha256(path.read_bytes()).hexdigest())
    return shas


# ── the report ───────────────────────────────────────────────────────────────

CONFIRMED_METHODS = ("four_fields", "unit_number", "decision")


def build_report(rows_by_source: dict[str, list[dict]], existing: list[dict], pairs: list[Pair],
                 ambiguous: Iterable[Ambiguity] = ()) -> dict:
    """Pure: the per-source numbers from harvested rows, the graph's records
    and the matcher's result. A row is ``confirmed`` when a CONFIRMED pair
    names it, ``review`` when it waits for a person, ``new`` otherwise."""
    ambiguous = list(ambiguous)
    review_reasons: dict[str, set[str]] = defaultdict(set)
    for a in ambiguous:
        review_reasons[a.auction_id].add(a.reason)
    graph_by_id = {r["auction_id"]: r for r in existing}
    graph_core = {aid: core_from_graph(r) for aid, r in graph_by_id.items()}
    incoming_ids = {row["auction_id"] for rows in rows_by_source.values() for row in rows}

    confirmed_pair: dict[str, Pair] = {}
    for p in pairs:
        if p.confidence != "CONFIRMED":
            continue
        for me in (p.a_id, p.b_id):
            if me in incoming_ids:
                confirmed_pair.setdefault(me, p)

    report: dict = {"sources": {}, "pairs": [p.__dict__ for p in pairs],
                    "ambiguous": [a.__dict__ for a in ambiguous]}
    for source, rows in rows_by_source.items():
        by_method = Counter()
        by_reason = Counter()
        fills = Counter()
        photos_new = photos_confirmed = 0
        new_complete = Counter()
        already = new = review = confirmed = 0
        confirmed_ids: list[dict] = []
        for row in rows:
            aid = row["auction_id"]
            if aid in graph_by_id:
                already += 1
                continue
            mine = core_from_row(row)
            p = confirmed_pair.get(aid)
            if p is None:
                if aid in review_reasons:
                    review += 1
                    for reason in sorted(review_reasons[aid]):
                        by_reason[reason] += 1
                    continue
                new += 1
                new_complete[sum(mine.values())] += 1
                photos_new += mine["has_photos"]
                continue
            confirmed += 1
            by_method[p.method] += 1
            other = p.b_id if p.a_id == aid else p.a_id
            theirs = graph_core.get(other)
            if theirs is None:                      # confirmed against another new portal's row
                theirs = core_from_row(next(r for rs in rows_by_source.values() for r in rs if r["auction_id"] == other))
            gained = [f for f in CORE_FIELDS if mine[f] and not theirs[f]]
            for f in gained:
                fills[f] += 1
            photos_confirmed += "has_photos" in gained
            confirmed_ids.append({"auction_id": aid, "matches": other, "method": p.method,
                                  "fills": gained, "evidence": p.evidence})
        n_new = sum(new_complete.values())
        report["sources"][source] = {
            "rows": len(rows),
            "already_loaded": already,
            "new": new,
            "review": review,
            "review_by_reason": dict(sorted(by_reason.items())),
            "confirmed": confirmed,
            "confirmed_by_method": {m: by_method.get(m, 0) for m in CONFIRMED_METHODS},
            "fills": {f: fills.get(f, 0) for f in CORE_FIELDS},
            "photos_gained": {"new": photos_new, "confirmed": photos_confirmed},
            "new_core_complete": {str(k): v for k, v in sorted(new_complete.items())},
            "new_core_avg": round(sum(k * v for k, v in new_complete.items()) / n_new, 1) if n_new else None,
            "confirmed_listings": confirmed_ids,
        }
    return report


def format_report(report: dict) -> str:
    lines = []
    for source, s in report["sources"].items():
        m = s["confirmed_by_method"]
        reasons = ", ".join(f"{r} {n}" for r, n in s["review_by_reason"].items())
        lines.append(f"{source}")
        lines.append(f"  rows {s['rows']}  already loaded {s['already_loaded']}  new {s['new']}"
                     f"  review {s['review']}{' (' + reasons + ')' if reasons else ''}"
                     f"  confirmed {s['confirmed']} (four_fields {m['four_fields']}, unit_number {m['unit_number']},"
                     f" decision {m['decision']})")
        if s["new"]:
            hist = ", ".join(f"{k}/9: {v}" for k, v in s["new_core_complete"].items())
            lines.append(f"  new listings core fields  avg {s['new_core_avg']}  [{hist}]")
        filled = {f: n for f, n in s["fills"].items() if n}
        if filled:
            lines.append("  fills on confirmed  " + ", ".join(f"{f} +{n}" for f, n in filled.items()))
        lines.append(f"  photos gained  new {s['photos_gained']['new']}  confirmed {s['photos_gained']['confirmed']}")
        for c in s["confirmed_listings"]:
            fills = f"  fills {', '.join(c['fills'])}" if c["fills"] else ""
            lines.append(f"    {c['auction_id']} ~ {c['matches']}  {c['method']:<12} {c['evidence']}{fills}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="append", default=None, help="limit to these data/listings/<source>.jsonl (repeatable)")
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    ap.add_argument("--downloads-dir", default=str(DOWNLOADS_DIR))
    ap.add_argument("--existing-json", default=None, help="read the graph's listings from this file instead of Neo4j")
    ap.add_argument("--save-existing", default=None, help="write the fetched graph listings here (for --existing-json later)")
    ap.add_argument("--json", default=None, help="write the full report here")
    args = ap.parse_args(argv)

    rows_by_source = load_rows(Path(args.data_dir), args.source)
    if not rows_by_source:
        print(f"no rows under {args.data_dir}/listings — run scripts/harvest_sources.py first", file=sys.stderr)
        return 1

    if args.existing_json:
        with open(args.existing_json, encoding="utf-8") as fh:
            existing = json.load(fh)
    else:
        existing = fetch_existing()
        if args.save_existing:
            with open(args.save_existing, "w", encoding="utf-8") as fh:
                json.dump(existing, fh, ensure_ascii=False, default=str)
    print(f"graph: {len(existing)} listings", file=sys.stderr)

    downloads_dir = Path(args.downloads_dir)
    incoming = [candidate_from_row(row, doc_shas=download_shas(row, downloads_dir))
                for rows in rows_by_source.values() for row in rows]
    result = match_listings(incoming, [graph_candidate(r) for r in existing])
    report = build_report(rows_by_source, existing, result.pairs, result.ambiguous)
    print(format_report(report))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
