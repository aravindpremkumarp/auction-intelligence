"""
scripts/report_duplicate_documents.py
-------------------------------------
Read-only diagnostic for issue #45: count and list AuctionProperty nodes
that have multiple :Document nodes sharing the same filename.

The pipeline and the R2 upload script have historically MERGE-d on
``file_path``, which is not a stable key (it varies between an absolute
filesystem path and a bare filename depending on whether the local file
exists at MERGE time, and the upload script writes the R2 storage_key
into the same field). The result is that the same logical file can end
up as two :Document nodes both linked to the property, which the UI
then renders twice.

Run standalone:
    python -m scripts.report_duplicate_documents
    python -m scripts.report_duplicate_documents --csv out.csv
    python -m scripts.report_duplicate_documents --auction-id AUC123
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.neo4j_client import run_query

REPORT_CYPHER = """
MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
WHERE ($auction_id IS NULL OR a.auction_id = $auction_id)
  AND d.filename IS NOT NULL
WITH a, d.filename AS filename, collect(d) AS docs
WHERE size(docs) > 1
RETURN a.auction_id                              AS auction_id,
       a.url                                     AS url,
       filename                                  AS filename,
       size(docs)                                AS dup_count,
       [x IN docs | x.file_path]                 AS file_paths,
       [x IN docs | x.public_url]                AS public_urls,
       [x IN docs | x.storage_key]               AS storage_keys
ORDER BY dup_count DESC, a.auction_id, filename
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auction-id", default=None,
                        help="Limit the report to a single auction_id.")
    parser.add_argument("--csv", default=None,
                        help="Write the full per-row report to this CSV path.")
    parser.add_argument("--limit", type=int, default=20,
                        help="Number of example rows to print to stdout (default: 20).")
    args = parser.parse_args()

    rows = run_query(REPORT_CYPHER, {"auction_id": args.auction_id})

    if not rows:
        print("No duplicate documents found.")
        return 0

    affected_auctions = {r["auction_id"] for r in rows}
    total_extra_nodes = sum(r["dup_count"] - 1 for r in rows)

    print("=" * 70)
    print("Duplicate :Document nodes (issue #45)")
    print("=" * 70)
    print(f"  Affected properties        : {len(affected_auctions)}")
    print(f"  (auction_id, filename) pairs: {len(rows)}")
    print(f"  Extra duplicate nodes      : {total_extra_nodes}")
    print()
    print(f"Top {min(args.limit, len(rows))} examples:")
    print("-" * 70)
    for r in rows[: args.limit]:
        print(f"  {r['auction_id']}  x{r['dup_count']}  {r['filename']}")
        if r.get("url"):
            print(f"    {r['url']}")

    if args.csv:
        out_path = Path(args.csv)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "auction_id", "url", "filename", "dup_count",
                "file_paths", "public_urls", "storage_keys",
            ])
            for r in rows:
                writer.writerow([
                    r["auction_id"], r.get("url") or "", r["filename"], r["dup_count"],
                    " | ".join(str(x or "") for x in (r.get("file_paths") or [])),
                    " | ".join(str(x or "") for x in (r.get("public_urls") or [])),
                    " | ".join(str(x or "") for x in (r.get("storage_keys") or [])),
                ])
        print()
        print(f"Full CSV written to {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
