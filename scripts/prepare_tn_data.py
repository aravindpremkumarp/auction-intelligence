"""
prepare_tn_data.py
------------------
Reads live_eauction_data.jsonl and produces:
  - tn_auction_data.jsonl          : cleaned Tamil Nadu-only records
  - listings/eauctionsindia.jsonl  : the same rows, where the other portals'
                                     adapters write theirs
  - tn_download_report.txt         : download coverage summary

The mapping itself now lives in ``sources/eauctionsindia.py`` — the adapter
that gives eauctionsindia the same contract as BAANKNET and bankeauctions.
This script is the weekly-run entry point over it, and keeps writing
``tn_auction_data.jsonl`` so ``scripts/load_tn_to_neo4j.py`` and everything
after it are untouched. The rows carry a few extra keys (``source``,
``documents`` …); nothing downstream reads keys it does not know.

Fixes applied (unchanged):
  1. Filter  : Province/State contains "Tamil Nadu"
  2. Price   : strip ₹ / â‚¹ mojibake, remove commas → float
  3. Dates   : parse "DD-MM-YYYY HHMM AM/PM" → ISO 8601 string
  4. Downloads: split on ; or , → list, remove N/A, validate files on disk

The previous, self-contained version is kept as
``scripts/legacy/prepare_tn_data_v1.py`` for one release so the two can be
diffed on the same input.
"""

import json
import os

from sources.eauctionsindia import EauctionsIndiaAdapter
from sources.eauctionsindia import split_downloads as _split_downloads
from sources.eauctionsindia import validate_downloads as _validate_downloads
from sources.normalize import clean_price, parse_date  # noqa: F401 — re-exported for scripts.backfill_reserve_price

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..')
INPUT_FILE   = os.path.join(PROJECT_ROOT, "data", "live_eauction_data.jsonl")
OUTPUT_FILE  = os.path.join(PROJECT_ROOT, "data", "tn_auction_data.jsonl")
LISTINGS_FILE = os.path.join(PROJECT_ROOT, "data", "listings", "eauctionsindia.jsonl")
REPORT_FILE  = os.path.join(PROJECT_ROOT, "data", "tn_download_report.txt")
DL_DIR       = os.path.join(PROJECT_ROOT, "downloads", "live_properties")
STATE_FILTER = "tamil nadu"


# ── Helpers kept for callers that import them from here ──────────────────────

def split_downloads(raw: str) -> list[str]:
    return _split_downloads(raw)


def validate_downloads(file_list: list[str]) -> tuple[list[str], list[str]]:
    return _validate_downloads(file_list, DL_DIR)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    adapter = EauctionsIndiaAdapter(state=STATE_FILTER, input_path=INPUT_FILE, download_dir=DL_DIR)

    tn_records = []
    total_input = 0
    total_dl_found = 0
    total_dl_missing = 0
    records_missing_dl = 0

    print(f"Reading {INPUT_FILE} ...")

    # Count every non-empty line, parseable or not, as the old script did.
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        total_input = sum(1 for line in f if line.strip())

    for raw in adapter.harvest():
        listing = adapter.normalize(raw)
        if listing is None:
            continue
        total_dl_found += len(listing.downloads_found)
        total_dl_missing += len(listing.downloads_missing)
        if listing.downloads_missing:
            records_missing_dl += 1
        tn_records.append(listing.to_row())

    # ── Write output JSONL (both places) ─────────────────────────────────────
    print(f"Writing {len(tn_records)} TN records to {OUTPUT_FILE} ...")
    os.makedirs(os.path.dirname(LISTINGS_FILE), exist_ok=True)
    for path in (OUTPUT_FILE, LISTINGS_FILE):
        with open(path, 'w', encoding='utf-8') as out:
            for rec in tn_records:
                out.write(json.dumps(rec, ensure_ascii=False) + '\n')

    # ── Write download report ─────────────────────────────────────────────────
    report_lines = [
        "Tamil Nadu Auction Data — Download Report",
        "=" * 50,
        f"Total input records        : {total_input:,}",
        f"Tamil Nadu records         : {len(tn_records):,}",
        "",
        f"Download files found       : {total_dl_found:,}",
        f"Download files missing     : {total_dl_missing:,}",
        f"Records with missing files : {records_missing_dl:,}",
        f"Records with N/A downloads : {sum(1 for r in tn_records if not r['downloads_list'])}",
        f"Records fully complete     : {sum(1 for r in tn_records if r['downloads_complete'])}",
        "",
        "--- Records with missing downloads ---",
    ]
    for rec in tn_records:
        if rec['downloads_missing']:
            report_lines.append(
                f"  {rec['auction_id']} | {rec['title'][:60]}"
            )
            for mf in rec['downloads_missing']:
                report_lines.append(f"      MISSING: {mf}")

    with open(REPORT_FILE, 'w', encoding='utf-8') as rpt:
        rpt.write('\n'.join(report_lines))

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("=" * 50)
    print(f"  Input records            : {total_input:,}")
    print(f"  Tamil Nadu records       : {len(tn_records):,}")
    print(f"  Download files found     : {total_dl_found:,}")
    print(f"  Download files missing   : {total_dl_missing:,}")
    print(f"  Records missing files    : {records_missing_dl:,}")
    print(f"  Output -> {OUTPUT_FILE}")
    print(f"  Listings -> {LISTINGS_FILE}")
    print(f"  Report -> {REPORT_FILE}")
    print("=" * 50)


if __name__ == "__main__":
    main()
