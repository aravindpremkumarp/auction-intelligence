"""
prepare_tn_data.py
------------------
Reads live_eauction_data.jsonl and produces:
  - tn_auction_data.jsonl  : cleaned Tamil Nadu-only records
  - tn_download_report.txt : download coverage summary

Fixes applied:
  1. Filter  : Province/State contains "Tamil Nadu"
  2. Price   : strip ₹ / â‚¹ mojibake, remove commas → float
  3. Dates   : parse "DD-MM-YYYY HHMM AM/PM" → ISO 8601 string
  4. Downloads: split on ; or , → list, remove N/A, validate files on disk
"""

import json
import os
import re
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..')
INPUT_FILE   = os.path.join(PROJECT_ROOT, "data", "live_eauction_data.jsonl")
OUTPUT_FILE  = os.path.join(PROJECT_ROOT, "data", "tn_auction_data.jsonl")
REPORT_FILE  = os.path.join(PROJECT_ROOT, "data", "tn_download_report.txt")
DL_DIR       = os.path.join(PROJECT_ROOT, "downloads", "live_properties")
STATE_FILTER = "tamil nadu"

# ── Helpers ───────────────────────────────────────────────────────────────────

def clean_price(raw: str) -> tuple[str, float | None]:
    """
    Returns (raw_string, numeric_float) from values like '₹1,23,456.00' or 'â‚¹1,23,456.00'.
    """
    if not raw:
        return raw, None
    # remove rupee symbol in both correct UTF-8 and mojibake forms
    cleaned = re.sub(r'(â‚¹|₹|\u20b9)', '', str(raw))
    cleaned = cleaned.replace(',', '').strip()
    try:
        return raw, float(cleaned)
    except ValueError:
        return raw, None


def parse_date(raw: str) -> str | None:
    """
    Parses 'DD-MM-YYYY HHMM AM/PM' → ISO 8601 'YYYY-MM-DDTHH:MM:SS'.
    Example: '23-03-2026 0130 PM' → '2026-03-23T13:30:00'
    Returns None if unparseable.
    """
    if not raw or raw.strip().lower() in ('', 'none', 'n/a'):
        return None
    raw = raw.strip()
    # Match pattern: DD-MM-YYYY HHMM AM/PM  (HHMM may be 3 or 4 digits)
    m = re.match(
        r'(\d{2})-(\d{2})-(\d{4})\s+(\d{3,4})\s*(AM|PM)',
        raw, re.IGNORECASE
    )
    if not m:
        return None
    day, month, year, hhmm, meridiem = m.groups()
    # Pad HHMM to 4 digits
    hhmm = hhmm.zfill(4)
    hour   = int(hhmm[:2])
    minute = int(hhmm[2:])
    # Convert 12-hour → 24-hour
    if meridiem.upper() == 'PM' and hour != 12:
        hour += 12
    elif meridiem.upper() == 'AM' and hour == 12:
        hour = 0
    try:
        dt = datetime(int(year), int(month), int(day), hour, minute)
        return dt.strftime('%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return None


def split_downloads(raw: str) -> list[str]:
    """
    Split download filenames on ';' or ',' and clean up.
    Filters out empty strings and 'N/A'.
    """
    if not raw or str(raw).strip().lower() in ('none', 'n/a', ''):
        return []
    parts = re.split(r'[;,]', str(raw))
    return [p.strip() for p in parts if p.strip() and p.strip().upper() != 'N/A']


def validate_downloads(file_list: list[str]) -> tuple[list[str], list[str]]:
    """Returns (found_files, missing_files)."""
    found, missing = [], []
    for fname in file_list:
        path = os.path.join(DL_DIR, fname)
        (found if os.path.exists(path) else missing).append(fname)
    return found, missing


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    tn_records      = []
    total_input     = 0
    total_dl_found  = 0
    total_dl_missing = 0
    records_missing_dl = 0

    print(f"Reading {INPUT_FILE} ...")

    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total_input += 1
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue

            # ── 1. Filter Tamil Nadu ──────────────────────────────────────
            state = str(r.get('Province/State', '')).strip()
            if STATE_FILTER not in state.lower():
                continue

            # Filter unwanted asset categories
            # "Gold Auctions" added for consistency with scripts/remove_non_property_categories.py
            # (2026-05-11), which already purged this category from the live Neo4j graph as a
            # non-property asset type — keeping the two filters in sync prevents a future scrape
            # of a Gold Auctions listing from silently flowing back into the graph.
            unwanted_cats = {"Vehicle Auctions", "Scrap, Plant & Machinery", "Others", "Gold Auctions"}
            if str(r.get('Asset Category', '')).strip() in unwanted_cats:
                continue

            # ── 2. Clean prices ───────────────────────────────────────────
            rp_raw, rp_num = clean_price(r.get('Reserve Price', ''))
            emd_raw, emd_num = clean_price(r.get('EMD', ''))

            # ── 3. Normalize dates ────────────────────────────────────────
            # Handle both old typo key and corrected key
            app_date_raw = r.get('Application Submission Date') or r.get('Application Subbmision Date', '')

            start_dt = parse_date(r.get('Auction Start Date', ''))
            end_dt   = parse_date(r.get('Auction End Time', ''))
            app_dt   = parse_date(app_date_raw)

            # ── 4. Fix downloads ──────────────────────────────────────────
            dl_list = split_downloads(r.get('Downloads', ''))
            found, missing = validate_downloads(dl_list)
            total_dl_found   += len(found)
            total_dl_missing += len(missing)
            if missing:
                records_missing_dl += 1

            # ── Build clean record ────────────────────────────────────────
            clean = {
                # Identity
                "auction_id"                : r.get('auction_id') or r.get('URL', '').rstrip('/').split('/')[-1],
                "url"                       : r.get('URL', ''),
                "title"                     : r.get('Title', ''),
                "description"               : r.get('Description', '').split('Province/State :')[0].strip(),
                # Financial
                "reserve_price_raw"         : rp_raw,
                "reserve_price_num"         : rp_num,
                "emd_raw"                   : emd_raw,
                "emd_num"                   : emd_num,
                # Dates
                "auction_start_date_raw"    : r.get('Auction Start Date', ''),
                "auction_start_dt"          : start_dt,
                "auction_end_time_raw"      : r.get('Auction End Time', ''),
                "auction_end_dt"            : end_dt,
                "application_deadline_raw"  : app_date_raw,
                "application_deadline_dt"   : app_dt,
                # Location
                "area"                      : r.get('Area/Town', ''),
                "city"                      : r.get('City/Town', ''),
                "state"                     : state,
                # Classification
                "asset_category"            : r.get('Asset Category', ''),
                "property_type_raw"         : r.get('Property Type', '') or '',
                "property_types"            : [
                    p.strip()
                    for p in (r.get('Property Type', '') or '').split(',')
                    if p.strip()
                ],
                # Scraper wrote 'Auction Type' on older runs and 'AuctionType' on newer
                # ones; read both or ~1/3 of records load with no AuctionType — and,
                # because the loader filters on it, no Borrower edge either.
                "auction_type"              : r.get('Auction Type') or r.get('AuctionType', ''),
                # Parties
                "bank_name"                 : r.get('Bank Name', ''),
                "branch_name"               : r.get('Branch Name', ''),
                "borrower_name"             : r.get('Borrower Name', ''),
                "service_provider"          : r.get('Service Provider', ''),
                "contact_details"           : r.get('Contact Details', ''),
                # Downloads
                "downloads_list"            : dl_list,
                "downloads_found"           : found,
                "downloads_missing"         : missing,
                "downloads_complete"        : len(missing) == 0,
            }

            tn_records.append(clean)

    # ── Write output JSONL ────────────────────────────────────────────────────
    print(f"Writing {len(tn_records)} TN records to {OUTPUT_FILE} ...")
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as out:
        for rec in tn_records:
            out.write(json.dumps(rec, ensure_ascii=False) + '\n')

    # ── Write download report ─────────────────────────────────────────────────
    report_lines = [
        "Tamil Nadu Auction Data — Download Report",
        "=" * 50,
        f"Total input records        : {total_input:,}",
        f"Tamil Nadu records         : {len(tn_records):,}",
        f"",
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
    print(f"  Report -> {REPORT_FILE}")
    print("=" * 50)


if __name__ == "__main__":
    main()
