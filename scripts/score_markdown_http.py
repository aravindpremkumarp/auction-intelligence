"""
scripts/score_markdown_http.py
------------------------------
Run the markdown-quality scorer over the Neo4j HTTP Query API instead of the
Bolt driver. Same algorithm as ``pipeline.score_markdown`` (it imports the very
same ``_score_one``) — this is purely an alternate transport for environments
where the Bolt port (7687) is firewalled but HTTPS (443) to Aura is open.

Run:
    python -m scripts.score_markdown_http --dry-run   # compute + show distribution, no writes
    python -m scripts.score_markdown_http             # compute and write markdown_quality_score
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import urllib.request

from pipeline.score_markdown import _score_one

READ_BATCH = 100
WRITE_BATCH = 200


def _endpoint() -> tuple[str, str]:
    host = os.environ["NEO4J_URI"].split("//", 1)[1].rstrip("/")
    db = os.environ.get("NEO4J_DATABASE", "neo4j")
    auth = base64.b64encode(
        f'{os.environ["NEO4J_USERNAME"]}:{os.environ["NEO4J_PASSWORD"]}'.encode()
    ).decode()
    return f"https://{host}/db/{db}/query/v2", auth


def query(statement: str, parameters: dict | None = None) -> list[list]:
    url, auth = _endpoint()
    body = json.dumps({"statement": statement, "parameters": parameters or {}}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": "Basic " + auth,
                 "Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["data"]["values"]


FETCH = """
MATCH (d:Document)
WHERE d.markdown IS NOT NULL AND d.markdown <> ''
OPTIONAL MATCH (d)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
OPTIONAL MATCH (a)-[:HAS_BORROWER]->(b:Borrower)
WITH d, a, collect(DISTINCT b.name) AS borrowers
WITH d, collect(CASE WHEN a IS NULL THEN NULL ELSE {
        reserve_price:       a.reserve_price_num,
        website_description: a.website_description,
        borrowers:           borrowers
     } END) AS props_raw
RETURN d.file_path AS file_path,
       d.markdown  AS markdown,
       [p IN props_raw WHERE p IS NOT NULL] AS properties
ORDER BY d.file_path
SKIP $skip LIMIT $limit
"""

WRITE = """
UNWIND $rows AS row
MATCH (d:Document {file_path: row.file_path})
SET d.markdown_quality_score     = row.score,
    d.markdown_quality_scored_at = datetime()
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="compute scores + print distribution, but don't write")
    args = ap.parse_args()

    total = query("MATCH (d:Document) WHERE d.markdown IS NOT NULL AND d.markdown<>'' "
                  "RETURN count(d) AS n")[0][0]
    print(f"Documents to score: {total}")

    results: list[dict] = []
    skip = 0
    while skip < total:
        rows = query(FETCH, {"skip": skip, "limit": READ_BATCH})
        for file_path, markdown, properties in rows:
            score = _score_one(markdown or "", properties or [])
            results.append({"file_path": file_path, "score": score})
        skip += READ_BATCH
        print(f"  scored {min(skip, total)}/{total}", end="\r")
    print()

    scored = [r["score"] for r in results if r["score"] is not None]
    unscored = len(results) - len(scored)
    if scored:
        buckets = {"0-49": 0, "50-69": 0, "70-89": 0, "90-100": 0}
        for s in scored:
            k = "0-49" if s < 50 else "50-69" if s < 70 else "70-89" if s < 90 else "90-100"
            buckets[k] += 1
        print(f"scored={len(scored)} unscored(NULL)={unscored} "
              f"min={min(scored):.1f} avg={sum(scored)/len(scored):.1f} max={max(scored):.1f}")
        print("distribution:", buckets)

    if args.dry_run:
        print("(dry-run) no writes performed.")
        return 0

    for i in range(0, len(results), WRITE_BATCH):
        query(WRITE, {"rows": results[i : i + WRITE_BATCH]})
        print(f"  wrote {min(i + WRITE_BATCH, len(results))}/{len(results)}", end="\r")
    print(f"\nDone. Wrote markdown_quality_score on {len(results)} Documents.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
