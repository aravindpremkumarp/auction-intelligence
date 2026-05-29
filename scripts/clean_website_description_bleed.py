"""
clean_website_description_bleed.py
----------------------------------
In-place cleanup: strip the glued-on location fields (Province/State,
City/Town, Area/Town) from `AuctionProperty.website_description` directly in
Neo4j — no JSONL source needed. Use this when the descriptions already live in
the graph (the scraper concatenates the State/City/Area node values onto the
end of the scraped prose).

Uses markdown_match.strip_field_bleed, so it matches exactly what the
markdown-quality score does to the probe.

Idempotent — a second run is a no-op (cleaned text has no label left to find).
Never touches `a.description`, so the OCR pipeline and human edits are safe.

Run:
    python -m scripts.clean_website_description_bleed --dry-run   # counts + before/after samples
    python -m scripts.clean_website_description_bleed             # write the cleaned text back
"""
from __future__ import annotations

import argparse

from api.neo4j_client import run_query, run_read_query
from api.review.markdown_match import strip_field_bleed

WRITE_CHUNK = 200

# Only nodes whose description still carries one of the glued location labels.
_MATCH = (
    "MATCH (a:AuctionProperty) "
    "WHERE a.website_description IS NOT NULL AND ("
    "  a.website_description CONTAINS 'Province/State' "
    "  OR a.website_description CONTAINS 'City/Town' "
    "  OR a.website_description CONTAINS 'Area/Town')"
)


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report counts + samples but don't write")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap to first N nodes (staged rollout)")
    args = parser.parse_args()

    rows = run_read_query(
        _MATCH + " RETURN a.auction_id AS aid, a.website_description AS d",
        {}, max_rows=200_000, timeout=120.0,
    )
    if args.limit:
        rows = rows[: args.limit]
    print(f"AuctionProperty nodes with location-field bleed: {len(rows):,}")

    # Compute the cleaned text; keep only rows that actually change.
    updates = []
    for r in rows:
        raw = r["d"]
        cleaned = strip_field_bleed(raw).strip()
        if cleaned and cleaned != raw:
            updates.append({"aid": r["aid"], "desc": cleaned, "raw": raw})
    print(f"Rows that will change:                            {len(updates):,}")

    if args.dry_run:
        print("\n--- before/after (tail 90 chars) ---")
        for u in updates[:8]:
            print(f"\n[{u['aid']}]")
            print(f"  before …{u['raw'][-90:]!r}")
            print(f"  after  …{u['desc'][-90:]!r}")
        print("\n(dry-run) no writes performed.")
        return 0

    written = 0
    for batch in chunked(updates, WRITE_CHUNK):
        run_query(
            """
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.aid})
            SET a.website_description = row.desc
            """,
            {"rows": batch},
        )
        written += len(batch)
        print(f"  wrote {written:,}/{len(updates):,}", end="\r")

    print(f"\nDone. Cleaned website_description on {written:,} nodes.")
    print("Next: python -m pipeline.score_markdown --force")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
