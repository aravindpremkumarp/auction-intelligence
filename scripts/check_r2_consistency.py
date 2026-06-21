"""
scripts/check_r2_consistency.py
-------------------------------
Guard against the multi-property-notice 404 class: a :Document whose
``public_url`` / ``storage_key`` points at an R2 object that does not exist.

A single sales-notice file is shared across the lots of a multi-property
notice. The enrichment pipeline creates a Document per auction keyed
``notices/{auction_id}/{filename}``, but the file is physically uploaded under
only one prefix and the dedupe/upload steps choose a canonical ``public_url``.
If a Document ends up pointing at a key whose object was never uploaded (or was
later cleaned up), the review UI's ``<img>`` 404s ("file missing — 404 in R2").

This script cross-checks the graph against the bucket and reports:

  * dangling   — Document.storage_key has no object in R2          (FAILURE)
  * mismatch   — Document.public_url != R2_PUBLIC_BASE_URL/key     (FAILURE)
  * no_key     — Document with neither storage_key nor public_url  (warning)
  * orphan     — R2 object under notices/ with no Document         (info)

Exit code is non-zero when any FAILURE rows exist, so it doubles as a CI /
cron guard. Reads use the same Neo4j + R2 config as the rest of the pipeline;
set ``NEO4J_HTTP_API=1`` to reach Neo4j Aura from behind an HTTP-only proxy.

Run standalone:
    python -m scripts.check_r2_consistency
    python -m scripts.check_r2_consistency --json
    python -m scripts.check_r2_consistency --show 20
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import storage
from api.neo4j_client import run_read_query

DOCS_CYPHER = """
MATCH (d:Document)
RETURN d.filename    AS filename,
       d.storage_key AS storage_key,
       d.public_url  AS public_url
"""


def list_r2_keys() -> set[str]:
    """Every object key currently in the public notices bucket."""
    client = storage.r2_client()
    paginator = client.get_paginator("list_objects_v2")
    keys: set[str] = set()
    for page in paginator.paginate(Bucket=storage.R2_BUCKET):
        for obj in page.get("Contents", []) or []:
            keys.add(obj["Key"])
    return keys


def check() -> dict:
    """Return a report dict cross-checking the graph against R2."""
    storage._require_config()
    base = storage.R2_PUBLIC_BASE_URL.rstrip("/")
    r2_keys = list_r2_keys()
    docs = run_read_query(DOCS_CYPHER, max_rows=1_000_000, timeout=60.0)

    dangling, mismatch, no_key = [], [], []
    referenced: set[str] = set()

    for d in docs:
        sk = d.get("storage_key")
        pu = d.get("public_url")
        if not sk and not pu:
            no_key.append(d)
            continue
        if sk:
            referenced.add(sk)
            if sk not in r2_keys:
                dangling.append(d)
        if sk and pu and pu != f"{base}/{sk}":
            mismatch.append(d)

    orphans = sorted(k for k in r2_keys if k not in referenced)

    return {
        "total_docs": len(docs),
        "total_r2_objects": len(r2_keys),
        "dangling": dangling,
        "mismatch": mismatch,
        "no_key": no_key,
        "orphans": orphans,
        "ok": not dangling and not mismatch,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="Emit the full report as JSON (for CI parsing).")
    parser.add_argument("--show", type=int, default=10,
                        help="How many example rows to print per category.")
    args = parser.parse_args()

    report = check()

    if args.json:
        print(json.dumps(report, default=str, indent=2))
    else:
        print(f"Documents       : {report['total_docs']}")
        print(f"R2 objects      : {report['total_r2_objects']}")
        print(f"Dangling (FAIL) : {len(report['dangling'])}")
        print(f"Mismatch (FAIL) : {len(report['mismatch'])}")
        print(f"No key (warn)   : {len(report['no_key'])}")
        print(f"Orphans (info)  : {len(report['orphans'])}")
        for label in ("dangling", "mismatch", "no_key"):
            rows = report[label]
            for d in rows[: args.show]:
                print(f"  [{label}] {d.get('filename')} "
                      f"key={d.get('storage_key')} url={d.get('public_url')}")
            if len(rows) > args.show:
                print(f"  … and {len(rows) - args.show} more {label}")

    if not report["ok"]:
        print("\nFAIL: documents reference R2 objects that do not exist.",
              file=sys.stderr)
        return 1
    print("\nOK: every Document.storage_key resolves to an R2 object.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
