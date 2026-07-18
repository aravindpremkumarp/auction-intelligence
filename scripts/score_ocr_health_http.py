"""
scripts/score_ocr_health_http.py
--------------------------------
Run the OCR-health scorer over the Neo4j HTTP Query API instead of the Bolt
driver. Same algorithm as ``pipeline.ocr_health`` (it imports the very same
``score_ocr_health``) — this is purely an alternate transport for environments
where the Bolt port (7687) is firewalled but HTTPS (443) to Aura is open.
Sibling of ``scripts/score_markdown_http.py``.

Run:
    python -m scripts.score_ocr_health_http --dry-run  # distribution only, no writes
    python -m scripts.score_ocr_health_http            # compute and write
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import time
import urllib.request
from collections import Counter

from pipeline.ocr_health import score_ocr_health

READ_BATCH = 100
WRITE_BATCH = 200
RETRIES = 4


def _endpoint() -> tuple[str, str]:
    host = os.environ["NEO4J_URI"].split("//", 1)[1].rstrip("/")
    db = os.environ.get("NEO4J_DATABASE", "neo4j")
    auth = base64.b64encode(
        f'{os.environ["NEO4J_USERNAME"]}:{os.environ["NEO4J_PASSWORD"]}'.encode()
    ).decode()
    return f"https://{host}/db/{db}/query/v2", auth


def query(statement: str, parameters: dict | None = None) -> list[list]:
    """POST one statement; retry transient transport errors (the agent-proxy
    occasionally drops a chunked read mid-body)."""
    url, auth = _endpoint()
    body = json.dumps({"statement": statement, "parameters": parameters or {}}).encode()
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Authorization": "Basic " + auth,
                         "Content-Type": "application/json",
                         "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)["data"]["values"]
        except Exception:
            if attempt == RETRIES - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


FETCH = """
MATCH (d:Document)
WHERE d.markdown IS NOT NULL AND d.markdown <> ''
RETURN d.file_path AS file_path, d.markdown AS markdown
ORDER BY d.file_path
SKIP $skip LIMIT $limit
"""

WRITE = """
UNWIND $rows AS row
MATCH (d:Document {file_path: row.file_path})
SET d.ocr_health_score = row.score,
    d.ocr_health_flags = row.flags,
    d.ocr_health_at    = datetime()
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
    flag_counts: Counter = Counter()
    skip = 0
    while skip < total:
        rows = query(FETCH, {"skip": skip, "limit": READ_BATCH})
        if not rows:
            break
        for file_path, markdown in rows:
            h = score_ocr_health(markdown or "")
            results.append({"file_path": file_path,
                            "score": h["score"], "flags": h["flags"]})
            for f in h["flags"]:
                flag_counts[f] += 1
        skip += READ_BATCH
        print(f"  scored {min(skip, total)}/{total}", end="\r")
    print()

    flagged = sum(1 for r in results if r["flags"])
    scored = [r["score"] for r in results if r["score"] is not None]
    buckets = {"0-49": 0, "50-69": 0, "70-89": 0, "90-100": 0}
    for s in scored:
        k = "0-49" if s < 50 else "50-69" if s < 70 else "70-89" if s < 90 else "90-100"
        buckets[k] += 1
    print(f"scored={len(results)}  flagged={flagged}  clean={len(results) - flagged}")
    print("score buckets:", buckets)
    print("flag breakdown:", dict(flag_counts))

    if args.dry_run:
        print("(dry-run) no writes performed.")
        return 0

    for i in range(0, len(results), WRITE_BATCH):
        query(WRITE, {"rows": results[i: i + WRITE_BATCH]})
        print(f"  wrote {min(i + WRITE_BATCH, len(results))}/{len(results)}", end="\r")
    print(f"\nDone. Wrote ocr_health_score/flags on {len(results)} Documents.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
