"""
build_spine.py — one :AuctionEvent per auction, rebuilt from the branches.

Reads every listing with its source properties, the notice lot behind it,
its media and its CONFIRMED / PROBABLE :SAME_LISTING_AS neighbours; groups
listings into clusters (union-find over those edges — an INFERRED match is
"possibly the same" and never merges two listings into one event); merges
each cluster with sources.merge.merge_event; then drops every :AuctionEvent
and recreates them with LISTS / ANNOUNCES / DEPICTS edges.

A pure function of the branches: run it again after a better matcher, a
corrected notice or a fresh harvest and the spine follows. Nothing on a
branch is ever modified.

    python -m scripts.build_spine --dry-run      # counts + core_complete histogram, no writes
    python -m scripts.build_spine
    NEO4J_HTTP_API=1 python -m scripts.build_spine --dry-run   # when Bolt is blocked
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sources.merge import event_node_props, merge_event  # noqa: E402

BATCH = 200

#: Only these edge grades merge listings into one event.
MERGE_GRADES = ("CONFIRMED", "PROBABLE")

FETCH_LISTINGS = """
MATCH (a:AuctionProperty)
OPTIONAL MATCH (a)-[:CONDUCTED_BY]->(bk:Bank)
OPTIONAL MATCH (a)-[:HAS_BORROWER]->(br:Borrower)
OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(c:City)
OPTIONAL MATCH (a)-[:LOCATED_IN_AREA]->(ar:Area)
OPTIONAL MATCH (a)-[:HAS_PROPERTY_TYPE]->(pt:PropertyType)
// collect() every 1:N edge so a listing with two banks or two cities is still one row
WITH a, collect(DISTINCT bk.name)[0] AS bank, collect(DISTINCT c.name)[0] AS city, collect(DISTINCT ar.name)[0] AS area,
     collect(DISTINCT br.name)[0] AS borrower, collect(DISTINCT pt.name) AS ptypes
CALL { WITH a OPTIONAL MATCH (a)-[:HAS_MEDIA]->(m:Media)
       RETURN [x IN collect(m {.url, .kind, .is_main}) WHERE x.url IS NOT NULL] AS media }
CALL { WITH a OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(d:Document)
       RETURN [x IN collect(d.filename) WHERE x IS NOT NULL] AS documents }
CALL { WITH a OPTIONAL MATCH (a)-[r:SAME_LISTING_AS]-(o:AuctionProperty)
       RETURN [x IN collect({id: o.auction_id, confidence: r.confidence}) WHERE x.id IS NOT NULL] AS neighbours }
CALL { WITH a OPTIONAL MATCH (a)-[:IS_LOT]->(l:Lot)
       OPTIONAL MATCH (l)-[:HAS_BOUNDARY]->(b:Boundary)
       OPTIONAL MATCH (l)-[hx:HAS_EXTENT]->(mx:Measurement)
       OPTIONAL MATCH (l)-[:POSSESSION_IS]->(p:PossessionType)
       OPTIONAL MATCH (l)-[:OFFERED_IN]->(au:Auction)
       OPTIONAL MATCH (l)-[:HAS_PARTY {role: 'borrower'}]->(lb:Borrower)
       WITH l, p, au, lb,
            [x IN collect(DISTINCT [b.side, b.adjacency_raw, b.measurement_ft]) WHERE x[0] IS NOT NULL] AS bounds,
            [x IN collect(DISTINCT [mx.kind, mx.sqft_norm, mx.raw, hx.is_headline]) WHERE x[0] IS NOT NULL] AS extents
       RETURN collect(DISTINCT {
           lot_key: l.lot_key, property_type: l.property_type, district: l.district,
           possession: coalesce(p.name, l.possession_stated), encumbrance: l.encumbrance,
           full_description: l.full_description, borrower: lb.name,
           reserve_price_num: au.reserve_price_num, emd_num: au.emd_num,
           auction_start_dt: toString(au.auction_start_dt), auction_end_dt: toString(au.auction_end_dt),
           bid_increment_num: au.bid_increment_num, attempt_no: au.attempt_no,
           bounds: bounds, extents: extents
       })[0] AS lot }
RETURN a.auction_id AS auction_id, coalesce(a.source, 'eauctionsindia') AS source,
       coalesce(a.source_rank, 3) AS source_rank, coalesce(a.source_url, a.url) AS source_url, a.url AS url,
       a.title AS title, coalesce(a.enriched_description, a.description) AS description,
       a.reserve_price_num AS reserve_price_num, a.emd_num AS emd_num,
       toString(a.auction_start_dt) AS auction_start_dt, toString(a.auction_end_dt) AS auction_end_dt,
       toString(a.application_deadline_dt) AS application_deadline_dt,
       toString(a.inspection_start_dt) AS inspection_start_dt, toString(a.inspection_end_dt) AS inspection_end_dt,
       a.auction_status AS auction_status, a.bid_increment_num AS bid_increment_num,
       a.portal_district AS portal_district, a.pincode AS pincode, a.possession_type AS possession_type,
       a.extent_raw AS extent_raw, a.borrower_address AS borrower_address,
       a.portal_property_type AS portal_property_type, ptypes AS property_types, a.property_type_raw AS property_type_raw,
       bank, borrower, city, area,
       a.revenue_district AS revenue_district, a.property_type_effective AS property_type_effective,
       a.total_area AS total_area, a.grounded_source_file AS grounded_source_file,
       a.boundary_north AS boundary_north, a.boundary_south AS boundary_south,
       a.boundary_east AS boundary_east, a.boundary_west AS boundary_west,
       a.boundary_measurement_north AS boundary_measurement_north, a.boundary_measurement_south AS boundary_measurement_south,
       a.boundary_measurement_east AS boundary_measurement_east, a.boundary_measurement_west AS boundary_measurement_west,
       media, documents, neighbours, lot
"""

DROP_EVENTS = "MATCH (e:AuctionEvent) DETACH DELETE e"

WRITE_EVENTS = """
UNWIND $rows AS r
CREATE (e:AuctionEvent)
SET e = r.props,
    e.auction_start_dt = CASE WHEN r.props.auction_start_dt IS NULL THEN NULL ELSE datetime(r.props.auction_start_dt) END,
    e.auction_end_dt   = CASE WHEN r.props.auction_end_dt   IS NULL THEN NULL ELSE datetime(r.props.auction_end_dt)   END,
    e.built_at         = datetime(r.props.built_at)
WITH e, r
UNWIND r.listing_ids AS lid
MATCH (a:AuctionProperty {auction_id: lid})
MERGE (a)-[:LISTS]->(e)
WITH DISTINCT e, r
CALL { WITH e, r
       UNWIND r.documents AS fn
       MATCH (d:Document {filename: fn})
       MERGE (d)-[:ANNOUNCES]->(e)
       RETURN count(*) AS docs }
CALL { WITH e, r
       UNWIND r.media_urls AS u
       MATCH (m:Media {url: u})
       MERGE (m)-[:DEPICTS]->(e)
       RETURN count(*) AS pics }
RETURN count(e) AS n
"""


class _UnionFind:
    def __init__(self, ids):
        self.parent = {i: i for i in ids}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def lot_from_record(lot: dict | None) -> dict | None:
    """The fetched lot map → the shape ``sources.merge.notice_branch`` reads:
    boundaries and measurements keyed by side, the headline extent."""
    if not lot or not lot.get("lot_key"):
        return None
    bounds, measures = {}, {}
    for side, adjacency, length in lot.get("bounds") or ():
        if side and adjacency and side not in bounds:
            bounds[side] = adjacency
        if side and length and side not in measures:
            measures[side] = length
    extent_sqft = extent_kind = extent_raw = None
    extents = lot.get("extents") or []
    headline = [e for e in extents if len(e) > 3 and e[3]] or extents
    if headline:
        kind, sqft, raw, _ = (list(headline[0]) + [None] * 4)[:4]
        extent_sqft, extent_kind, extent_raw = sqft, kind, raw
    return {k: v for k, v in lot.items() if k not in ("bounds", "extents")} | {
        "boundaries": bounds, "measurements": measures,
        "extent_sqft": extent_sqft, "extent_kind": extent_kind, "extent_raw": extent_raw}


def cluster_listings(rows: list[dict]) -> list[list[dict]]:
    """Group listings joined by CONFIRMED / PROBABLE :SAME_LISTING_AS edges;
    every other listing is its own cluster. Deterministic order."""
    by_id = {r["auction_id"]: r for r in rows}
    uf = _UnionFind(by_id)
    for r in rows:
        for n in r.get("neighbours") or ():
            if n.get("confidence") in MERGE_GRADES and n.get("id") in by_id:
                uf.union(r["auction_id"], n["id"])
    groups: dict[str, list[dict]] = defaultdict(list)
    for aid in sorted(by_id):
        groups[uf.find(aid)].append(by_id[aid])
    return [groups[k] for k in sorted(groups)]


def cluster_confidence(cluster: list[dict]) -> str:
    if len(cluster) == 1:
        return "SINGLE"
    ids = {r["auction_id"] for r in cluster}
    grades = [n["confidence"] for r in cluster for n in (r.get("neighbours") or ())
              if n.get("id") in ids and n.get("confidence") in MERGE_GRADES]
    return "CONFIRMED" if grades and all(g == "CONFIRMED" for g in grades) else "PROBABLE"


def build_events(rows: list[dict], *, built_at: str | None = None) -> list[dict]:
    """Every listing row → one merged event per cluster, as write rows:
    ``{props, listing_ids, documents, media_urls}``."""
    out = []
    for cluster in cluster_listings(rows):
        lots = [lot for lot in (lot_from_record(r.get("lot")) for r in cluster) if lot]
        media, seen = [], set()
        for r in cluster:
            for m in r.get("media") or ():
                if m.get("url") and m["url"] not in seen:
                    seen.add(m["url"])
                    media.append(m)
        ev = merge_event({"listings": cluster, "lots": lots, "media": media,
                          "confidence": cluster_confidence(cluster)}, built_at=built_at)
        docs = sorted({fn for r in cluster for fn in (r.get("documents") or ())})
        out.append({"props": event_node_props(ev), "listing_ids": ev["listing_ids"],
                    "documents": docs, "media_urls": [m["url"] for m in media]})
    return out


def summarize(events: list[dict]) -> str:
    n = len(events)
    sizes = Counter(len(e["listing_ids"]) for e in events)
    core = Counter(e["props"]["core_complete"] for e in events)
    conf = Counter(e["props"]["confidence"] for e in events)
    photos = sum(1 for e in events if e["props"]["has_photos"])
    lines = [f"events: {n}  (from {sum(len(e['listing_ids']) for e in events)} listings)",
             "  cluster sizes: " + ", ".join(f"{k} listing{'s' if k > 1 else ''}: {v}" for k, v in sorted(sizes.items())),
             "  confidence:    " + ", ".join(f"{k} {v}" for k, v in sorted(conf.items())),
             "  core_complete: " + ", ".join(f"{k}/9: {core.get(k, 0)}" for k in range(10)),
             f"  avg core {sum(k * v for k, v in core.items()) / n:.1f}/9;  with photos {photos}" if n else "  (no events)"]
    return "\n".join(lines)


def run(dry_run: bool = False) -> int:
    from api.neo4j_client import run_query

    t0 = time.monotonic()
    rows = run_query(FETCH_LISTINGS)
    print(f"  {len(rows)} listings fetched in {time.monotonic() - t0:.0f}s")
    events = build_events(rows)
    print(summarize(events))
    if dry_run:
        print("[dry-run] no writes")
        return 0
    run_query(DROP_EVENTS)
    written = 0
    for i in range(0, len(events), BATCH):
        run_query(WRITE_EVENTS, {"rows": events[i:i + BATCH]})
        written += len(events[i:i + BATCH])
        print(f"  wrote {written}/{len(events)}", end="\r")
    print(f"\n  {written} events written in {time.monotonic() - t0:.0f}s")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="build and summarise, write nothing")
    args = ap.parse_args(argv)
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
