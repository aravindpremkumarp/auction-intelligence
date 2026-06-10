"""Apply multi-property descriptions from cache to Neo4j.

Mirrors scripts/_apply_v3_descs.py for the multi case:

- Pull every (AuctionProperty, Document) pair where notice_type='multi'.
- For each Document, load its cached schedules array.
- Match each linked listing to a schedule by reserve_price_num (exact;
  ±1% fallback if no exact hit).
- Write description + source='notice' in chunks of 200 with retries.
- Log unmatched listings to data/multi_splitter_unmatched.csv for review.

Idempotent: re-running rewrites the same descriptions to the same listings
(unless the cache changed). Apply stage is intentionally separate from
extract stage so cache problems can be fixed without re-calling the LLM.
"""
from __future__ import annotations

import csv
import json
import pathlib
from collections import defaultdict
from dotenv import load_dotenv

from api.neo4j_client import run_query, run_read_query


load_dotenv("e:/01_vibe_coding/08_auction/.env")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "pipeline" / "cache" / "notice_descriptions_v3_multi"
UNMATCHED_CSV = REPO_ROOT / "data" / "multi_splitter_unmatched.csv"

PRICE_TOLERANCE_PCT = 1.0  # ±1% fallback when no exact reserve_price match
WRITE_CHUNK = 200


def safe_name(fp: str) -> str:
    return fp.replace("/", "_").replace("\\", "_").replace(":", "_")


def match_schedule(reserve_price_num: int | None,
                   schedules: list[dict]) -> tuple[dict | None, str]:
    """Find the schedule whose reserve_price_num matches the listing's.

    Returns (schedule, match_reason). match_reason is one of:
      'exact'        — exact reserve_price_num match
      'tolerance'    — within ±1%
      'none'         — no acceptable match
      'no_listing_price' — listing has no reserve_price_num
    """
    if reserve_price_num is None:
        return None, "no_listing_price"
    exact = [s for s in schedules
             if s.get("reserve_price_num") == reserve_price_num]
    if len(exact) == 1:
        return exact[0], "exact"
    if len(exact) > 1:
        # Tie-break: longest description wins (more content = more specific)
        return max(exact, key=lambda s: len(s["property_description_full"])), "exact"
    # Tolerance fallback
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


def main() -> int:
    print(f"Loading cached schedules from {CACHE_DIR}")
    if not CACHE_DIR.exists():
        print("Cache directory does not exist. Run extract_multi_descriptions first.")
        return 1

    # Pull listings + their notice + the listing's reserve price
    rows = run_read_query("""
      MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document {notice_type: 'multi'})
      RETURN a.auction_id        AS aid,
             a.reserve_price_num AS price,
             d.file_path         AS fp
    """, max_rows=20_000)
    print(f"Multi-property listing-document pairs: {len(rows)}")

    # Group by document
    by_doc: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_doc[r["fp"]].append(r)

    payload: list[dict] = []
    unmatched: list[dict] = []
    stats = {
        "exact": 0, "tolerance": 0, "none": 0,
        "no_listing_price": 0, "cache_missing": 0,
        "cache_empty": 0, "cache_parse_fail": 0,
    }

    for fp, listings in by_doc.items():
        cache_path = CACHE_DIR / f"{safe_name(fp)}.json"
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

    print()
    print(f"Match stats:")
    print(f"  exact:             {stats['exact']:>5}")
    print(f"  tolerance (±1%):   {stats['tolerance']:>5}")
    print(f"  no match:          {stats['none']:>5}")
    print(f"  no listing price:  {stats['no_listing_price']:>5}")
    print(f"  cache missing:     {stats['cache_missing']:>5}")
    print(f"  cache empty:       {stats['cache_empty']:>5}")
    print(f"  cache parse fail:  {stats['cache_parse_fail']:>5}")
    print(f"  --- writable: {len(payload)} listings ---")
    print(f"  --- unmatched: {len(unmatched)} listings ---")
    print()

    # Write unmatched to CSV for review
    if unmatched:
        UNMATCHED_CSV.parent.mkdir(parents=True, exist_ok=True)
        with UNMATCHED_CSV.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["auction_id", "file_path", "listing_price",
                             "reason", "schedule_prices_in_cache"])
            for u in unmatched:
                writer.writerow([u["aid"], u["fp"], u["price"], u["reason"],
                                 ";".join(str(p) for p in u.get("schedule_prices", []))])
        print(f"Unmatched logged to {UNMATCHED_CSV}")
        print()

    # Apply payload in chunks with retries
    written = 0
    for i in range(0, len(payload), WRITE_CHUNK):
        batch = payload[i:i + WRITE_CHUNK]
        for attempt in range(3):
            try:
                run_query("""
                    UNWIND $rows AS row
                    MATCH (a:AuctionProperty {auction_id: row.auction_id})
                    SET a.description = row.desc,
                        a.description_source = 'notice'
                """, {"rows": batch})
                written += len(batch)
                break
            except Exception as e:
                print(f"  [neo4j retry {attempt + 1}] {type(e).__name__}: {e}")
                if attempt == 2:
                    print(f"  [neo4j FAIL] gave up on chunk @ {i}")

    print(f"\nWrote {written} listings")
    print()
    print("description_source distribution after apply:")
    for r in run_read_query(
        "MATCH (a:AuctionProperty) RETURN a.description_source AS src, count(*) AS n ORDER BY src"
    ):
        print(f"  {str(r['src']):<20} {r['n']:>5}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
