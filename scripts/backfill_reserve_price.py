"""Backfill reserve price onto listings the 'ReservePrice' key miss left empty.

prepare_tn_data read only the spaced ``Reserve Price`` key. The portal began
rendering ``ReservePrice`` (no space) in June 2026, so every listing scraped
off one of those pages loaded with no price at all -- while its EMD came
through intact, because ``EMD`` is spelled the same either way. That asymmetry
is what made the gap read as a portal omission instead of a key miss.

The values were never lost: they sit in the raw scrape JSONL. This reads them
back and writes the two fields prepare_tn_data would have written.

Only fills listings whose reserve_price_num IS NULL -- an existing price is
never overwritten, so a re-run is idempotent and this can never clobber a
value the loader got right.

Does NOT re-run downstream work. Lot resolution and the price-agreement
comparison both key off reserve price, so re-run those after this lands:
    python -m scripts.resolve_lots

Run:
    NEO4J_HTTP_API=1 python -m scripts.backfill_reserve_price --dry-run
    NEO4J_HTTP_API=1 python -m scripts.backfill_reserve_price
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter

from api.neo4j_client import run_query, run_read_query
from scripts.prepare_tn_data import clean_price

JSONL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "live_eauction_data.jsonl")
WRITE_CHUNK = 200
#: Read both spellings, newest first -- same precedence as prepare_tn_data.
PRICE_KEYS = ("Reserve Price", "ReservePrice")


def priceless_listings() -> dict[str, str]:
    """url -> auction_id for every listing with no reserve price."""
    rows = run_read_query("""
        MATCH (ap:AuctionProperty)
        WHERE ap.reserve_price_num IS NULL AND ap.url IS NOT NULL
        RETURN ap.auction_id AS aid, ap.url AS url
    """, {}, max_rows=100_000)
    return {r["url"]: r["aid"] for r in rows}


def harvest(targets: dict[str, str], path: str) -> tuple[list[dict], Counter]:
    """Scan the raw scrape for a price on each target url.

    A url can appear more than once (re-scrapes). Later records win, matching
    the loader, which replays the file in order.
    """
    stats = Counter()
    found: dict[str, dict] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                stats["unparsable_lines"] += 1
                continue
            url = rec.get("URL")
            if url not in targets:
                continue
            stats["records_seen"] += 1
            raw = next((rec[k] for k in PRICE_KEYS if rec.get(k)), "")
            price_raw, price_num = clean_price(raw)
            if price_num is None:
                stats["no_usable_price"] += 1
                continue
            stats["key_" + ("spaced" if rec.get(PRICE_KEYS[0]) else "unspaced")] += 1
            found[url] = {"aid": targets[url], "raw": price_raw, "num": price_num}
    return list(found.values()), stats


def write_rows(rows: list[dict]) -> int:
    written = 0
    for i in range(0, len(rows), WRITE_CHUNK):
        batch = rows[i:i + WRITE_CHUNK]
        res = run_query("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.aid})
            WHERE a.reserve_price_num IS NULL
            SET a.reserve_price_raw = row.raw,
                a.reserve_price_num = row.num,
                a.reserve_price_source = 'backfill:ReservePrice-key'
            RETURN a.auction_id AS aid
        """, {"rows": batch})
        written += len(res) if res else 0
        print(f"  wrote {written:,}/{len(rows):,}", end="\r")
    print()
    return written


def report(rows: list[dict], targets: dict[str, str], stats: Counter) -> None:
    print(f"\n  listings with no price : {len(targets)}")
    print(f"  recovered from scrape  : {len(rows)}")
    print(f"  still unrecoverable    : {len(targets) - len(rows)}")
    for k, v in sorted(stats.items()):
        print(f"    {k:<20} {v}")
    if rows:
        print("\n  sample:")
        for r in rows[:5]:
            print(f"    {r['aid']:>7}  {r['raw']}  -> {r['num']:,.0f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jsonl", default=JSONL, help="raw scrape file")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be written and exit")
    args = ap.parse_args()

    targets = priceless_listings()
    if not targets:
        print("No listing is missing a reserve price -- nothing to do.")
        return 0
    rows, stats = harvest(targets, args.jsonl)
    report(rows, targets, stats)

    if args.dry_run:
        print("\n  --dry-run: nothing written.")
        return 0
    if not rows:
        print("\n  nothing recoverable; nothing written.")
        return 0
    written = write_rows(rows)
    print(f"  backfilled {written:,} listings.")
    print("  next: python -m scripts.resolve_lots  (lot matching keys off price)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
