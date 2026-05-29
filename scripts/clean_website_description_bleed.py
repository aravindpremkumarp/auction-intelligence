"""
clean_website_description_bleed.py
----------------------------------
In-place cleanup: strip the glued-on location fields (Province/State,
City/Town, Area/Town) from an AuctionProperty's description fields directly in
Neo4j — no JSONL source needed. The scraper concatenates the State/City/Area
node values onto the end of the scraped prose, and that text gets copied into
three fields:

    website_description   — probe used by the markdown-quality score
    description_scraped   — audit copy shown in the review "Website description" panel
    description           — the working description (when sourced from the website)

Uses markdown_match.strip_field_bleed, so it matches exactly what the
markdown-quality score does to the probe. The strip is purely subtractive (it
only removes the trailing label run), so it never corrupts real prose.

Idempotent — a second run is a no-op.

Run:
    python -m scripts.clean_website_description_bleed --dry-run   # counts + before/after samples
    python -m scripts.clean_website_description_bleed             # write the cleaned text back
"""
from __future__ import annotations

import argparse

from api.neo4j_client import run_query, run_read_query
from api.review.markdown_match import strip_field_bleed

WRITE_CHUNK = 200
FIELDS = ["website_description", "description_scraped", "description"]


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _clean_field(field: str, dry_run: bool, limit: int | None) -> int:
    rows = run_read_query(
        f"MATCH (a:AuctionProperty) "
        f"WHERE a.{field} CONTAINS 'Province/State' "
        f"   OR a.{field} CONTAINS 'City/Town' OR a.{field} CONTAINS 'Area/Town' "
        f"RETURN a.auction_id AS aid, a.{field} AS d",
        {}, max_rows=200_000, timeout=120.0,
    )
    if limit:
        rows = rows[:limit]

    updates = []
    for r in rows:
        raw = r["d"]
        cleaned = strip_field_bleed(raw).strip()
        if cleaned and cleaned != raw:
            updates.append({"aid": r["aid"], "val": cleaned, "raw": raw})

    print(f"[{field}] with bleed: {len(rows):,}  will change: {len(updates):,}")

    if dry_run:
        for u in updates[:3]:
            print(f"    [{u['aid']}] …{u['raw'][-70:]!r}")
            print(f"             → …{u['val'][-70:]!r}")
        return 0

    written = 0
    for batch in chunked(updates, WRITE_CHUNK):
        run_query(
            f"UNWIND $rows AS row "
            f"MATCH (a:AuctionProperty {{auction_id: row.aid}}) "
            f"SET a.{field} = row.val",
            {"rows": batch},
        )
        written += len(batch)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report counts + samples but don't write")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap each field to first N nodes (staged rollout)")
    args = parser.parse_args()

    total = 0
    for field in FIELDS:
        total += _clean_field(field, args.dry_run, args.limit)

    if args.dry_run:
        print("\n(dry-run) no writes performed.")
    else:
        print(f"\nDone. Cleaned {total:,} field values across {FIELDS}.")
        print("Next: python -m pipeline.score_markdown --force")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

