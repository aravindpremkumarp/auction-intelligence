"""Backfill the normalized property-type taxonomy onto :AuctionProperty.

Writes six fields per listing:

    property_type_raw       verbatim LangExtract value, kept so a rule change
                            can be re-run without re-extracting
    property_type_norm      one of pipeline.property_taxonomy.BUCKETS
    property_type_source    'reviewer' (the notice's extraction is verified),
                            'langextract', or 'none' (nothing extracted)
    asset_category_norm     derived from the bucket, not from the portal
    portal_property_type    the auction portal's :PropertyType name
    property_type_conflict  true when both sides name a bucket and disagree

The portal value is recorded but never used to fill a gap. Its "Land"/"Plot"
entries are the listing form's default and match the notice text only 34-54%
of the time, so a listing with no extracted type is written UNKNOWN/'none'
and left for re-extraction rather than being given a coin-flip label.

Lot-to-listing matching is imported from pipeline.apply_extractions — a
multi-lot notice backs several listings, and reserve price is what pairs them,
so this script must not re-derive that logic.

Pure re-classification of already-persisted entities: no LLM call, no
re-extraction. Free to run, safe to re-run, idempotent.

Run:
    python -m scripts.backfill_property_type --dry-run
    python -m scripts.backfill_property_type
"""

from __future__ import annotations

import argparse
from collections import Counter

from api.neo4j_client import run_query, run_read_query
from pipeline.apply_extractions import (
    entities_with_corrections,
    group_lots,
    match_lots_to_listings,
)
from pipeline.property_taxonomy import (
    UNKNOWN,
    asset_category,
    classify_portal_type,
    classify_property_type,
    is_conflict,
)

WRITE_CHUNK = 200


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_portal_types() -> dict[str, str]:
    """{auction_id: portal :PropertyType name} for every listing that has one.

    A handful of listings carry two :HAS_PROPERTY_TYPE edges; take the
    alphabetically first name so repeated runs agree with each other."""
    rows = run_read_query(
        "MATCH (a:AuctionProperty)-[:HAS_PROPERTY_TYPE]->(t:PropertyType) "
        "RETURN a.auction_id AS aid, t.name AS name",
        max_rows=50_000, timeout=120.0)
    out: dict[str, str] = {}
    for r in rows:
        aid, name = r.get("aid"), r.get("name")
        if aid and (aid not in out or (name or "") < out[aid]):
            out[aid] = name
    return out


def fetch_all_listing_ids() -> list[str]:
    rows = run_read_query(
        "MATCH (a:AuctionProperty) RETURN a.auction_id AS aid",
        max_rows=50_000, timeout=120.0)
    return [r["aid"] for r in rows if r.get("aid")]


def fetch_work(limit: int | None = None) -> list[dict]:
    """Documents with a grounded extraction + the listings they back."""
    return run_read_query(
        "MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document) "
        "WHERE d.extraction_json IS NOT NULL "
        "RETURN d.filename AS filename, "
        "       d.extraction_json AS extraction_json, "
        "       d.extraction_corrections_json AS corrections_json, "
        "       d.extraction_review_status AS review_status, "
        "       collect({aid: a.auction_id, price: a.reserve_price_num, "
        "                emd: a.emd_num, "
        "                borrowers: [(a)-[:HAS_BORROWER]->(bo) | bo.name]}) "
        "       AS listings "
        "ORDER BY d.filename"
        + (f" LIMIT {int(limit)}" if limit else ""),
        max_rows=20_000, timeout=120.0)


_SOURCE_RANK = {"reviewer": 2, "langextract": 1, "none": 0}


def _rank(row: dict) -> tuple[int, int]:
    """Better rows sort higher. A listing linked to more than one notice
    produces a row per notice; keep the one that actually names a type, and
    prefer a reviewer-verified notice over an unreviewed one."""
    return (1 if row["raw"] else 0, _SOURCE_RANK.get(row["source"], 0))


def build_rows(work: list[dict],
               portal: dict[str, str]) -> tuple[list[dict], Counter]:
    """One row per listing reachable from an extracted notice.

    A listing whose lot carries no property_type still gets a row, so the
    UNKNOWN/'none' state is written explicitly rather than left absent.
    """
    stats: Counter = Counter()
    rows: list[dict] = []

    for w in work:
        ents = entities_with_corrections(w["extraction_json"],
                                         w.get("corrections_json"))
        if not ents:
            stats["empty_extraction"] += 1
            continue
        source = ("reviewer" if w.get("review_status") == "verified"
                  else "langextract")
        lots = group_lots(ents)
        listings = [l for l in (w.get("listings") or []) if l.get("aid")]
        matches, unmatched = match_lots_to_listings(lots, listings)

        for listing, lot, reason in matches:
            raw = lot["fields"].get("property_type_raw")
            bucket = classify_property_type(raw) if raw else UNKNOWN
            portal_name = portal.get(listing["aid"])
            rows.append({
                "aid": listing["aid"],
                "raw": raw,
                "norm": bucket,
                "source": source if raw else "none",
                "category": asset_category(bucket, raw),
                "portal": portal_name,
                "conflict": is_conflict(bucket,
                                        classify_portal_type(portal_name)),
            })
            stats[f"bucket_{bucket}"] += 1
            if not raw:
                stats["no_property_type_on_lot"] += 1

        for listing, reason in unmatched:
            stats[f"unmatched_{reason}"] += 1

    best: dict[str, dict] = {}
    for row in rows:
        cur = best.get(row["aid"])
        if cur is None or _rank(row) > _rank(cur):
            best[row["aid"]] = row
    stats["duplicate_rows_collapsed"] = len(rows) - len(best)
    return list(best.values()), stats


def portal_only_rows(covered: set[str],
                     portal: dict[str, str]) -> list[dict]:
    """Listings no extracted notice reached: portal value for provenance,
    UNKNOWN type, source 'none'. No conflict — there is nothing to conflict
    with."""
    return [{
        "aid": aid,
        "raw": None,
        "norm": UNKNOWN,
        "source": "none",
        "category": asset_category(UNKNOWN, None),
        "portal": portal.get(aid),
        "conflict": False,
    } for aid in fetch_all_listing_ids() if aid not in covered]


def write_rows(rows: list[dict]) -> int:
    written = 0
    for batch in chunked(rows, WRITE_CHUNK):
        res = run_query("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.aid})
            SET a.property_type_raw = row.raw,
                a.property_type_norm = row.norm,
                a.property_type_source = row.source,
                a.asset_category_norm = row.category,
                a.portal_property_type = row.portal,
                a.property_type_conflict = row.conflict
            RETURN a.auction_id AS aid
        """, {"rows": batch})
        written += len(res) if res else 0
        print(f"  wrote {written:,}/{len(rows):,}", end="\r")
    return written


def report(rows: list[dict], stats: Counter) -> None:
    buckets = Counter(r["norm"] for r in rows)
    cats = Counter(r["category"] for r in rows)
    sources = Counter(r["source"] for r in rows)
    conflicts = sum(1 for r in rows if r["conflict"])

    print(f"\nlistings classified: {len(rows):,}")
    print("\n  property_type_norm")
    for b, n in buckets.most_common():
        print(f"    {b:<14} {n:>6,}")
    print("\n  asset_category_norm")
    for c, n in cats.most_common():
        print(f"    {c:<14} {n:>6,}")
    print("\n  property_type_source")
    for s, n in sources.most_common():
        print(f"    {s:<14} {n:>6,}")
    print(f"\n  conflicts with portal: {conflicts:,}")

    # Why a listing ends up UNKNOWN matters: an unmatched listing means the
    # notice WAS extracted but reserve price could not pair its lot to this
    # listing, which is a matching problem, not an extraction gap.
    unmatched = {k[len("unmatched_"):]: v for k, v in stats.items()
                 if k.startswith("unmatched_")}
    if unmatched:
        print(f"  listings a notice could not be paired to: "
              f"{sum(unmatched.values()):,} {unmatched}")
    if stats.get("no_property_type_on_lot"):
        print(f"  paired but the lot named no type: "
              f"{stats['no_property_type_on_lot']:,}")
    for key in ("empty_extraction", "duplicate_rows_collapsed"):
        if stats.get(key):
            print(f"  {key.replace('_', ' ')}: {stats[key]:,}")


def run(limit: int | None = None, dry_run: bool = False) -> int:
    portal = fetch_portal_types()
    print(f"listings with a portal property type: {len(portal):,}")

    work = fetch_work(limit)
    print(f"documents with a grounded extraction: {len(work):,}")

    rows, stats = build_rows(work, portal)
    covered = {r["aid"] for r in rows}
    if limit is None:
        extra = portal_only_rows(covered, portal)
        if extra:
            print(f"listings not reached by any extraction: {len(extra):,}"
                  f"  (written UNKNOWN/'none')")
            rows.extend(extra)

    report(rows, stats)

    if dry_run:
        print("\n(dry-run) no writes performed.")
        return 0

    written = write_rows(rows)
    print(f"\nDone. Backfilled property taxonomy on {written:,} listing(s).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N Documents (skips the portal-only pass)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the distribution without writing to Neo4j")
    args = ap.parse_args()
    return run(limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
