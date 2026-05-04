"""Build an Excel review of the multi-property splitter's output.

Matches the column format of data/mineru_pilot_review.xlsx so the same
review workflow (eyeball comparison of scraped vs LLM-extracted text +
markdown side by side) works here.

One row per multi-property AuctionProperty listing. The matcher links
the listing to its schedule in the LLM cache by reserve_price_num.

Columns (mirroring mineru_pilot_review.xlsx):
  auction_id, url, filename, current_source,
  len_scraped, len_v2, len_v3, len_md,
  scraped, v2_gemini_only, v3_mineru_llm, mineru_markdown

Plus splitter-specific extras after the pilot columns:
  reserve_price, notice_file_path, property_count, schedule_count,
  match_kind

Output: data/multi_splitter_review.xlsx
"""
from __future__ import annotations

import json
import pathlib
from dotenv import load_dotenv

import pandas as pd

from api.neo4j_client import run_read_query


load_dotenv("e:/01_vibe_coding/08_auction/.env")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "pipeline" / "cache" / "notice_descriptions_v3_multi"
OUTPUT = REPO_ROOT / "data" / "multi_splitter_review.xlsx"
# When the primary file is locked (open in Excel on Windows), fall back to
# a timestamped sibling so we never silently fail.
OUTPUT_FALLBACK = REPO_ROOT / "data" / "multi_splitter_review_FULL.xlsx"

PRICE_TOLERANCE_PCT = 1.0


def safe_name(fp: str) -> str:
    return fp.replace("/", "_").replace("\\", "_").replace(":", "_")


def find_match(price, schedules):
    """Return (schedule_dict, match_kind). Same matcher used by _apply_multi_descs."""
    if price is None:
        return None, "no_listing_price"
    exact = [s for s in schedules if s.get("reserve_price_num") == price]
    if len(exact) == 1:
        return exact[0], "exact"
    if len(exact) > 1:
        return max(exact, key=lambda s: len(s["property_description_full"])), "exact_dup"
    if price > 0:
        tol = price * PRICE_TOLERANCE_PCT / 100.0
        near = [s for s in schedules
                if isinstance(s.get("reserve_price_num"), int)
                and abs(s["reserve_price_num"] - price) <= tol]
        if near:
            return max(near, key=lambda s: len(s["property_description_full"])), "tolerance"
    return None, "none"


def main() -> int:
    if not CACHE_DIR.exists():
        print(f"Cache directory missing: {CACHE_DIR}")
        return 1

    # Pull every multi-property listing with its notice metadata
    rows = run_read_query("""
      MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document {notice_type: 'multi'})
      RETURN a.auction_id        AS auction_id,
             a.url               AS url,
             a.reserve_price_num AS reserve_price,
             a.description       AS scraped,
             a.description_source AS current_source,
             d.filename          AS filename,
             d.file_path         AS file_path,
             d.markdown          AS mineru_markdown,
             d.property_count    AS property_count
    """, max_rows=20_000)

    print(f"Multi-property listings: {len(rows)}")

    # Load schedules from cache
    out_rows = []
    cache_hit = 0
    for r in rows:
        fp = r["file_path"]
        cache_path = CACHE_DIR / f"{safe_name(fp)}.json"
        schedules = []
        schedule_count = 0
        match_kind = "cache_missing"
        v3 = ""
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                schedules = cached.get("schedules") or []
                schedule_count = len(schedules)
                cache_hit += 1
            except Exception as e:
                match_kind = f"cache_parse_fail: {e}"
        if schedules:
            sched, match_kind = find_match(r["reserve_price"], schedules)
            if sched:
                v3 = sched.get("property_description_full") or ""

        scraped = r["scraped"] or ""
        md = r["mineru_markdown"] or ""

        out_rows.append({
            "auction_id":     r["auction_id"],
            "url":            r["url"] or "",
            "filename":       r["filename"] or "",
            "current_source": r["current_source"] or "",
            "len_scraped":    len(scraped),
            "len_v2":         0,                # v2 (Gemini-only) was never run on multi notices
            "len_v3":         len(v3),
            "len_md":         len(md),
            "scraped":        scraped,
            "v2_gemini_only": "",                # column kept for format parity with pilot xlsx
            "v3_mineru_llm":  v3,
            "mineru_markdown": md,
            # Splitter-specific extras
            "reserve_price":     r["reserve_price"],
            "notice_file_path":  fp,
            "property_count":    r["property_count"],
            "schedule_count":    schedule_count,
            "match_kind":        match_kind,
        })

    df = pd.DataFrame(out_rows)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    target = OUTPUT
    try:
        # Probe write-lock by opening for append — fails on Windows if the
        # file is open in Excel.
        with target.open("ab") as _f:
            pass
    except PermissionError:
        print(f"[note] {target.name} is locked (open in Excel?); writing to "
              f"{OUTPUT_FALLBACK.name} instead.")
        target = OUTPUT_FALLBACK

    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="multi_compare", index=False)

        # Auto-size columns (capped so big text columns stay readable)
        sheet = writer.sheets["multi_compare"]
        for col_cells in sheet.columns:
            cells = list(col_cells)
            col_letter = cells[0].column_letter
            max_len = min(60, max(
                (len(str(c.value)) if c.value is not None else 0) for c in cells
            ))
            sheet.column_dimensions[col_letter].width = max(8, max_len + 2)

    # Summary
    total = len(df)
    by_kind = df["match_kind"].value_counts().to_dict()
    print(f"\nWrote {target}")
    print(f"  rows: {total}")
    print(f"  cache_hits: {cache_hit} of {total} listings (notices that have been extracted)")
    print(f"  match_kind distribution:")
    for k, v in sorted(by_kind.items(), key=lambda x: -x[1]):
        print(f"    {k:<25} {v:>5}  ({v*100//total}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
