"""
backfill_website_description.py
--------------------------------
One-off backfill: copies the website-scraped description from
data/tn_auction_data.jsonl onto the AuctionProperty node as
`website_description`, so the review UI can show it next to the
notice-extracted `description`.

The scraped description has the structured key-value fields glued onto its
end (the scraper grabs the whole container after the "Description" header),
e.g. "…land and buildingProvince/State :Tamil NaduCity/Town :Ranipet…". We
strip that trailing field bleed (see markdown_match.strip_field_bleed) before
storing, so both the review UI and the markdown-quality score work off the
clean description text.

Idempotent — safe to re-run. Never touches `a.description`, so it does
not disturb the OCR pipeline or human edits.

Run:
    python -m scripts.backfill_website_description --dry-run   # show before/after samples
    python -m scripts.backfill_website_description
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from api.neo4j_client import run_query, run_read_query
from api.review.markdown_match import strip_field_bleed

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUT_FILE = REPO_ROOT / "data" / "tn_auction_data.jsonl"
WRITE_CHUNK = 200


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def load_rows() -> list[dict]:
    rows: list[dict] = []
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            aid = (r.get("auction_id") or "").strip()
            desc = r.get("description")
            if not aid or not desc:
                continue
            raw = str(desc).strip()
            cleaned = strip_field_bleed(raw).strip()
            rows.append({"auction_id": aid, "desc": cleaned, "raw": raw})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report counts but don't write to Neo4j")
    args = parser.parse_args()

    rows = load_rows()
    print(f"JSONL rows with auction_id + description: {len(rows):,}")

    bled = [r for r in rows if r["desc"] != r["raw"]]
    print(f"Rows with field-bleed to strip:            {len(bled):,}")

    aids = [r["auction_id"] for r in rows]
    matched = run_read_query(
        "MATCH (a:AuctionProperty) WHERE a.auction_id IN $aids "
        "RETURN count(a) AS n",
        {"aids": aids},
    )
    n_matched = matched[0]["n"] if matched else 0
    print(f"Matching AuctionProperty nodes in Neo4j:    {n_matched:,}")

    if args.dry_run:
        print("\n--- field-bleed strip samples (before → after tail) ---")
        for r in bled[:5]:
            print(f"\n[{r['auction_id']}]")
            print(f"  before …{r['raw'][-90:]!r}")
            print(f"  after  …{r['desc'][-90:]!r}")
        print("\n(dry-run) no writes performed.")
        return 0

    written = 0
    for batch in chunked(rows, WRITE_CHUNK):
        run_query(
            """
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.auction_id})
            SET a.website_description = row.desc
            """,
            {"rows": batch},
        )
        written += len(batch)
        print(f"  wrote {written:,}/{len(rows):,}", end="\r")

    print(f"\nDone. Backfilled website_description on {written:,} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
