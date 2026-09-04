#!/usr/bin/env python3
"""Strip borrower/contact-column bleed from notice descriptions in Neo4j.

The multi-notice splitter sometimes swept the borrower/guarantor/contact column
into a lot's ``a.description`` (e.g. "Borrower:Mr X ... Residing at ... <real
property description>"). This cleans those rows in place using the shared
``strip_contact_prefix`` helper, backing up the original into
``a.description_precleanup`` so the change is reversible.

Only ``description_source='notice'`` rows are touched, and only when the helper
actually finds a borrower/contact block in front of a property-description
anchor — clean rows are left untouched.

    NEO4J_HTTP_API=1 python -m scripts.clean_description_bleed --dry-run
    NEO4J_HTTP_API=1 python -m scripts.clean_description_bleed
"""
from __future__ import annotations

import argparse
import sys

from api.neo4j_client import run_query, run_read_query
from api.review.markdown_match import strip_contact_prefix

WRITE_CHUNK = 200


def find_affected() -> list[dict]:
    rows = run_read_query(
        """
        MATCH (a:AuctionProperty)
        WHERE a.description_source = 'notice' AND a.description IS NOT NULL
        RETURN a.auction_id AS auction_id, a.description AS description
        """,
        {}, timeout=120, max_rows=100000,
    )
    out = []
    for r in rows:
        cleaned, changed = strip_contact_prefix(r["description"])
        if changed and cleaned.strip():
            out.append({"auction_id": r["auction_id"], "cleaned": cleaned})
    return out


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def persist(rows: list[dict]) -> int:
    written = 0
    for batch in chunked(rows, WRITE_CHUNK):
        # Back up the original once (description_precleanup is only set if it is
        # still null), then overwrite a.description with the cleaned text.
        result = run_query(
            """
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.auction_id})
            SET a.description_precleanup = coalesce(a.description_precleanup, a.description),
                a.description = row.cleaned
            RETURN a.auction_id AS aid
            """,
            {"rows": batch},
        )
        written += len(result) if result else 0
    return written


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report affected rows + before/after without writing")
    args = ap.parse_args()

    affected = find_affected()
    print(f"affected notice rows with borrower/contact bleed: {len(affected)}")
    if args.dry_run:
        for r in affected[:8]:
            print(f"  {r['auction_id']}: -> {r['cleaned'][:90]!r}")
        if len(affected) > 8:
            print(f"  ... and {len(affected) - 8} more")
        return 0
    if not affected:
        print("nothing to clean.")
        return 0
    written = persist(affected)
    print(f"cleaned {written} rows (original backed up to a.description_precleanup)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
