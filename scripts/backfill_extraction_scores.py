"""
backfill_extraction_scores.py
------------------------------
One-off backfill: scores every :Document that already has extraction_json but
no extraction_score — i.e. notices extracted by pipeline/load_extractions.py
(or scripts/reset_langextract_and_extract.py) before that write path started
computing pipeline/validators.validate()'s 0-100 quality score.

Pure re-validation of already-persisted entities — NO LLM call, no
re-extraction — so this is free to run and safe to re-run. Uses
validators.validate_stored, the same shim extract_batch.py's --from-graph
report uses, so the score matches exactly what that report would show.

Idempotent by default: only scores Documents where extraction_score IS NULL.
--force rescores everything (e.g. after a validators.py penalty change).

Run:
    python -m scripts.backfill_extraction_scores --dry-run   # counts + sample only
    python -m scripts.backfill_extraction_scores
    python -m scripts.backfill_extraction_scores --force
"""

from __future__ import annotations

import argparse
import json

from api.neo4j_client import run_query, run_read_query
from pipeline.validators import validate_stored

WRITE_CHUNK = 200


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def load_unscored(force: bool) -> list[dict]:
    where = "d.extraction_json IS NOT NULL"
    if not force:
        where += " AND d.extraction_score IS NULL"
    return run_read_query(
        f"MATCH (d:Document) WHERE {where} "
        "RETURN d.filename AS filename, d.markdown AS md, "
        "       d.extraction_json AS ej ORDER BY d.filename",
        max_rows=20_000, timeout=120.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="report counts but don't write to Neo4j")
    parser.add_argument("--force", action="store_true",
                        help="rescore Documents that already have extraction_score")
    args = parser.parse_args()

    docs = load_unscored(args.force)
    print(f"documents to score: {len(docs):,}"
          f"{' (forced rescore)' if args.force else ' (missing extraction_score)'}")
    if not docs:
        print("nothing to do.")
        return 0

    rows = []
    failed = 0
    for d in docs:
        try:
            ents = json.loads(d["ej"] or "[]")
        except json.JSONDecodeError:
            failed += 1
            continue
        score = validate_stored(ents, source_text=d["md"] or "")["score"]
        rows.append({"filename": d["filename"], "score": score})

    if failed:
        print(f"  [skip] {failed} document(s) had unparseable extraction_json")

    mean = round(sum(r["score"] for r in rows) / len(rows), 1) if rows else 0
    print(f"scored {len(rows):,} document(s), mean_score={mean}")

    if args.dry_run:
        print("\n--- sample ---")
        for r in rows[:10]:
            print(f"  {r['score']:>3}  {r['filename']}")
        print("\n(dry-run) no writes performed.")
        return 0

    written = 0
    for batch in chunked(rows, WRITE_CHUNK):
        run_query(
            """
            UNWIND $rows AS row
            MATCH (d:Document {filename: row.filename})
            SET d.extraction_score = row.score
            """,
            {"rows": batch},
        )
        written += len(batch)
        print(f"  wrote {written:,}/{len(rows):,}", end="\r")

    print(f"\nDone. Backfilled extraction_score on {written:,} document(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
