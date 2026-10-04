"""Put a document's previous extraction back.

Every write through pipeline/extraction_store keeps the read it replaced in
``extraction_prev_json`` (with the reader and time that made it). This
script swaps it back in — for one reader's writes (``--reader v2``, the
rollback after a cutover that went wrong), or for named documents — and
moves the reviewer's corrections back through the same re-anchoring the
forward write used (pipeline/extraction_ids), so nothing a person wrote is
lost in either direction. ``orphaned:*`` keys are never deleted.

Run:  NEO4J_HTTP_API=1 python -m scripts.revert_extraction --reader v2 --dry-run
      NEO4J_HTTP_API=1 python -m scripts.revert_extraction --only a.jpg b.jpg
"""
from __future__ import annotations

import argparse
import json
import sys

from api.neo4j_client import run_query, run_read_query
from pipeline.extraction_ids import carry_corrections
from pipeline.key_entities import stamp_key_scores
from pipeline.validators import SCORE_VERSION, validate_stored

SELECT = """
MATCH (d:Document)
WHERE d.extraction_prev_json IS NOT NULL AND ({predicate})
RETURN d.filename AS fn, coalesce(d.stitched_markdown, d.markdown) AS md,
       d.extraction_json AS cur, d.extraction_prev_json AS prev,
       d.extraction_prev_reader AS prev_reader, d.extraction_corrections_json AS cj,
       d.extraction_reader AS reader,
       coalesce(d.stitched_expected_lot_count, d.expected_lot_count) AS elc
{limit}
"""

REVERT = """
MATCH (d:Document {filename: $fn})
SET d.extraction_json = $prev, d.extraction_reader = $prev_reader,
    d.extraction_corrections_json = $cj,
    d.extraction_prev_json = $cur, d.extraction_prev_reader = $reader,
    d.extraction_prev_at = d.extraction_at,
    d.extraction_at = datetime(), d.extraction_score = $score,
    d.extraction_score_version = $score_version,
    d.extraction_review_status = 'pending', d.extraction_reverted_at = datetime()
REMOVE d.extraction_verified_by, d.extraction_verified_at,
       d.extraction_contested, d.extraction_fuzzy, d.extraction_illegible, d.extraction_dropped
RETURN d.filename
"""


def select(reader: str | None, only: list[str] | None, limit: int | None) -> list[dict]:
    if only:
        predicate = "d.filename IN $only"
    elif reader:
        predicate = "d.extraction_reader = $reader"
    else:
        raise SystemExit("give --reader or --only")
    return run_read_query(SELECT.format(predicate=predicate, limit=f"LIMIT {int(limit)}" if limit else ""),
                          {"reader": reader, "only": only or []}, max_rows=50_000, timeout=300.0)


def revert(row: dict, dry_run: bool) -> dict:
    try:
        prev = json.loads(row["prev"] or "[]")
    except json.JSONDecodeError:
        return {"fn": row["fn"], "status": "skip: previous read unreadable"}
    cj, report = carry_corrections(row.get("cur"), row.get("cj"), prev)
    score = validate_stored(prev, source_text=row.get("md") or "",
                            expected_lot_count=row.get("elc"))["score"]
    if not dry_run:
        run_query(REVERT, {"fn": row["fn"], "prev": row["prev"], "prev_reader": row.get("prev_reader") or "langextract",
                           "cj": cj, "cur": row.get("cur"), "reader": row.get("reader"),
                           "score": score, "score_version": SCORE_VERSION})
        stamp_key_scores([row["fn"]])
    return {"fn": row["fn"], "status": "reverted" if not dry_run else "would revert",
            "score": score, "corrections": report}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reader", help="revert every document last written by this reader")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    rows = select(args.reader, args.only, args.limit)
    print(f"{len(rows)} document(s) selected")
    for r in rows:
        print(json.dumps(revert(r, args.dry_run)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
