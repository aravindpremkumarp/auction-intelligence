"""
scripts/_recover_missing_markdown.py
------------------------------------
One-shot recovery: download every Document whose markdown is missing from
R2 to ``downloads/tn_properties/`` so the existing OCR pipeline can pick it
up via ``find_disk_path``.

Also backfills ``d.file_path`` from ``d.storage_key`` where null (the
global dedupe coalesce missed it for 94 rows that had no file_path on the
canonical), since the OCR cache key is derived from file_path.

Run:  python -m scripts._recover_missing_markdown
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.neo4j_client import run_read_query, run_query
from pipeline.config import DOWNLOADS_DIR


def main() -> int:
    target_dir = DOWNLOADS_DIR / "tn_properties"
    target_dir.mkdir(parents=True, exist_ok=True)

    # Backfill file_path from storage_key for missing-markdown docs that lack it.
    backfilled = run_query("""
      MATCH (d:Document)
      WHERE (d.markdown IS NULL OR d.markdown = '')
        AND d.file_path IS NULL
        AND d.storage_key IS NOT NULL
      SET d.file_path = d.storage_key
      RETURN count(d) AS n
    """)
    print(f"Backfilled file_path on {backfilled[0]['n']} Documents.")

    rows = run_read_query("""
      MATCH (d:Document)
      WHERE (d.markdown IS NULL OR d.markdown = '')
        AND d.public_url IS NOT NULL
        AND d.filename IS NOT NULL
      RETURN d.filename AS filename, d.public_url AS url
    """, max_rows=500)

    print(f"Downloading {len(rows)} files from R2 -> {target_dir}")
    ok, skipped, failed = 0, 0, 0
    for i, r in enumerate(rows, 1):
        dest = target_dir / r["filename"]
        if dest.exists() and dest.stat().st_size > 0:
            skipped += 1
            continue
        try:
            resp = requests.get(r["url"], timeout=30)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            ok += 1
        except Exception as e:
            failed += 1
            print(f"  [fail] {r['filename']}: {e}")
        if i % 25 == 0:
            print(f"  [{i}/{len(rows)}] ok={ok} skipped={skipped} failed={failed}")

    print()
    print(f"Done: downloaded={ok} already_present={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
