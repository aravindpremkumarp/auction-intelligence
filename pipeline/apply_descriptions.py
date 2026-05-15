"""Apply cached descriptions to :AuctionProperty.description in Neo4j.

For each :Document with cached schedules / single-property descriptions,
write the description onto every linked :AuctionProperty — UNLESS the
property has been human-edited (description_source='human') or human-
verified (description_verified=true). Those are protected against
pipeline-driven overwrites.

Match strategies:
  notice_type='single' -> one description per Document, applied to every
                          linked listing.
  notice_type='multi'  -> match each listing's reserve_price_num to a
                          schedule's reserve_price_num (exact, then ±1%
                          tolerance, then tie-break by description length).
                          Unmatched listings are logged to
                          data/multi_splitter_unmatched.csv.

After every successful apply pass, the Document's
``description_extraction_status`` is set to 'applied'.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys
from collections import defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv

from api.neo4j_client import run_query, run_read_query
from pipeline.config import (
    NOTICE_DESC_SINGLE_DIR,
    NOTICE_DESC_MULTI_DIR,
)


load_dotenv()

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
UNMATCHED_CSV = REPO_ROOT / "data" / "multi_splitter_unmatched.csv"

PRICE_TOLERANCE_PCT = 1.0
WRITE_CHUNK = 200


def safe_name(fp: str) -> str:
    return fp.replace("/", "_").replace("\\", "_").replace(":", "_")


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def match_schedule(reserve_price_num,
                   schedules: list[dict]) -> tuple[dict | None, str]:
    """Pick the schedule whose reserve_price_num matches a listing's.

    Returns (schedule, reason). reason ∈
      'exact' | 'tolerance' | 'none' | 'no_listing_price'
    """
    if reserve_price_num is None:
        return None, "no_listing_price"
    exact = [s for s in schedules
             if s.get("reserve_price_num") == reserve_price_num]
    if len(exact) == 1:
        return exact[0], "exact"
    if len(exact) > 1:
        return max(exact, key=lambda s: len(s["property_description_full"])), "exact"
    if reserve_price_num <= 0:
        return None, "none"
    tol = reserve_price_num * PRICE_TOLERANCE_PCT / 100.0
    near = [s for s in schedules
            if isinstance(s.get("reserve_price_num"), int)
            and abs(s["reserve_price_num"] - reserve_price_num) <= tol]
    if len(near) == 1:
        return near[0], "tolerance"
    if len(near) > 1:
        return max(near, key=lambda s: len(s["property_description_full"])), "tolerance"
    return None, "none"


def write_descriptions(payload: list[dict]) -> int:
    """UPSERT descriptions, skipping rows the human has already edited or
    verified. Returns the count of rows actually written.

    The MATCH+WHERE guard means a re-run can never clobber a human's edit
    or a human's verification — the only way to overwrite those is to
    explicitly unverify the row first (review API)."""
    if not payload:
        return 0
    written = 0
    for batch in chunked(payload, WRITE_CHUNK):
        result = run_query("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.auction_id})
            WHERE coalesce(a.description_verified, false) = false
              AND coalesce(a.description_source, '') <> 'human'
            SET a.description = row.desc,
                a.description_source = 'notice'
            RETURN a.auction_id AS aid
        """, {"rows": batch})
        written += len(result) if result else 0
    return written


def stamp_applied_status(file_paths: list[str]) -> None:
    """After writing descriptions for a Document, set its status='applied'."""
    if not file_paths:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    for batch in chunked(file_paths, 200):
        run_query("""
            UNWIND $fps AS fp
            MATCH (d:Document {file_path: fp})
            SET d.description_extraction_status = 'applied',
                d.description_extracted_at      = datetime($at)
        """, {"fps": batch, "at": now_iso})


def apply_single() -> tuple[int, list[str]]:
    """Returns (rows_written, applied_file_paths)."""
    rows = run_read_query("""
      MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document {notice_type: 'single'})
      RETURN a.auction_id AS aid, d.file_path AS fp
    """, max_rows=20_000)
    print(f"  single listing-document pairs: {len(rows)}")

    by_fp: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        by_fp[r["fp"]].append(r["aid"])

    payload: list[dict] = []
    applied: list[str] = []
    cache_missing = 0
    cache_empty = 0

    for fp, aids in by_fp.items():
        p = NOTICE_DESC_SINGLE_DIR / f"{safe_name(fp)}.json"
        if not p.exists():
            cache_missing += 1
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            cache_empty += 1
            continue
        desc = d.get("property_description_full")
        if not isinstance(desc, str) or not desc.strip():
            cache_empty += 1
            continue
        for aid in aids:
            payload.append({"auction_id": aid, "desc": desc})
        applied.append(fp)

    print(f"  cache_missing={cache_missing}  cache_empty={cache_empty}  "
          f"writable={len(payload)}")
    written = write_descriptions(payload)
    print(f"  wrote {written} listings (verified+human rows skipped)")
    return written, applied


def apply_multi() -> tuple[int, list[str]]:
    """Returns (rows_written, applied_file_paths)."""
    rows = run_read_query("""
      MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document {notice_type: 'multi'})
      RETURN a.auction_id        AS aid,
             a.reserve_price_num AS price,
             d.file_path         AS fp
    """, max_rows=30_000)
    print(f"  multi listing-document pairs: {len(rows)}")

    by_doc: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_doc[r["fp"]].append(r)

    payload: list[dict] = []
    applied: list[str] = []
    unmatched: list[dict] = []
    stats = {
        "exact": 0, "tolerance": 0, "none": 0,
        "no_listing_price": 0, "cache_missing": 0,
        "cache_empty": 0, "cache_parse_fail": 0,
    }

    for fp, listings in by_doc.items():
        cache_path = NOTICE_DESC_MULTI_DIR / f"{safe_name(fp)}.json"
        if not cache_path.exists():
            stats["cache_missing"] += len(listings)
            for l in listings:
                unmatched.append({"aid": l["aid"], "fp": fp, "price": l["price"],
                                  "reason": "cache_missing"})
            continue
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception as e:
            stats["cache_parse_fail"] += len(listings)
            for l in listings:
                unmatched.append({"aid": l["aid"], "fp": fp, "price": l["price"],
                                  "reason": f"cache_parse_fail: {e}"})
            continue
        schedules = cached.get("schedules") or []
        if not schedules:
            stats["cache_empty"] += len(listings)
            for l in listings:
                unmatched.append({"aid": l["aid"], "fp": fp, "price": l["price"],
                                  "reason": "cache_empty"})
            continue
        any_matched = False
        for l in listings:
            sched, reason = match_schedule(l["price"], schedules)
            stats[reason] += 1
            if sched is None:
                unmatched.append({"aid": l["aid"], "fp": fp, "price": l["price"],
                                  "reason": reason,
                                  "schedule_prices": [s.get("reserve_price_num")
                                                       for s in schedules]})
                continue
            payload.append({"auction_id": l["aid"],
                            "desc": sched["property_description_full"]})
            any_matched = True
        if any_matched:
            applied.append(fp)

    print(f"  match stats: exact={stats['exact']}  "
          f"tolerance={stats['tolerance']}  none={stats['none']}  "
          f"no_listing_price={stats['no_listing_price']}")
    print(f"  cache_missing={stats['cache_missing']}  "
          f"cache_empty={stats['cache_empty']}  "
          f"cache_parse_fail={stats['cache_parse_fail']}")
    print(f"  writable: {len(payload)} listings  unmatched: {len(unmatched)} listings")

    if unmatched:
        UNMATCHED_CSV.parent.mkdir(parents=True, exist_ok=True)
        with UNMATCHED_CSV.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["auction_id", "file_path", "listing_price",
                             "reason", "schedule_prices_in_cache"])
            for u in unmatched:
                writer.writerow([u["aid"], u["fp"], u["price"], u["reason"],
                                 ";".join(str(p) for p in u.get("schedule_prices", []))])
        print(f"  unmatched logged to {UNMATCHED_CSV}")

    written = write_descriptions(payload)
    print(f"  wrote {written} listings (verified+human rows skipped)")
    return written, applied


def print_summary() -> None:
    print()
    print("description_source distribution after apply:")
    for r in run_read_query(
        "MATCH (a:AuctionProperty) RETURN a.description_source AS src, "
        "count(*) AS n ORDER BY src"
    ):
        print(f"  {str(r['src']):<22} {r['n']:>5}")


def run() -> int:
    print("Apply single-property descriptions")
    s_written, s_applied = apply_single()

    print()
    print("Apply multi-property descriptions")
    m_written, m_applied = apply_multi()

    stamp_applied_status(s_applied + m_applied)

    print()
    print(f"Total: {s_written + m_written} listings updated  "
          f"({len(s_applied) + len(m_applied)} Documents stamped 'applied')")
    print_summary()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()
    return run()


if __name__ == "__main__":
    sys.exit(main())
