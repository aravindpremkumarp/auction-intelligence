"""
backfill_extraction_scores.py
------------------------------
Re-levels stored scores: scores every :Document that has extraction_json but no
extraction_score, and rescores every Document whose extraction_score_version is
behind pipeline.validators.SCORE_VERSION — notices extracted before that write
path stamped a score at all, and notices scored by an older validators.py whose
penalties have since changed.

Pure re-validation of already-persisted entities — NO LLM call, no
re-extraction — so this is free to run and safe to re-run. Uses
validators.validate_stored, the same shim extract_batch.py's --from-graph
report uses, so the score matches exactly what that report would show.

Run it after ANY validators.py change that bumps SCORE_VERSION: until it has,
the corpus holds two scales that look identical, and any comparison across them
(mean score, a score_min filter in the review queue) is meaningless.

Idempotent: a second run finds nothing, because the first stamped the current
version on everything it scored. --force rescores every extracted Document
regardless of version — for a validators.py edit that moves scores without a
version bump, which should not happen.

Run:
    python -m scripts.backfill_extraction_scores --dry-run   # counts + sample only
    python -m scripts.backfill_extraction_scores
    python -m scripts.backfill_extraction_scores --force
"""

from __future__ import annotations

import argparse
import json

from api.neo4j_client import run_query, run_read_query
from pipeline.key_entities import stamp_key_scores
from pipeline.validators import SCORE_VERSION, validate_stored

WRITE_CHUNK = 200


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def load_unscored(force: bool) -> list[dict]:
    """Documents needing a score: never scored, or scored on an older scale.

    coalesce(version, 0) treats a missing version as "before versioning" — a
    score written when validators.py had no SCORE_VERSION, which is exactly the
    stale case, not a fresh one.

    A follower page of a stitched notice (stitched_into) is excluded for the same
    reason it is never extracted on its own: its text rides in the leader's
    stitched_markdown, so there is nothing here to score.
    """
    where = "d.extraction_json IS NOT NULL AND d.stitched_into IS NULL"
    if not force:
        # extraction_key_score and the failure-filter inputs
        # (extraction_issue_codes / extraction_lot_count, all stamped by
        # pipeline/key_entities.stamp_key_scores) ride alongside: a document
        # never given them is picked up here too, so one backfill levels all.
        where += (" AND (d.extraction_score IS NULL"
                  "      OR coalesce(d.extraction_score_version, 0) < $version"
                  "      OR d.extraction_key_score IS NULL"
                  "      OR d.extraction_issue_codes IS NULL)")
    return run_read_query(
        f"MATCH (d:Document) WHERE {where} "
        "RETURN d.filename AS filename, "
        "       coalesce(d.stitched_markdown, d.markdown) AS md, "
        "       d.extraction_json AS ej ORDER BY d.filename",
        {"version": SCORE_VERSION}, max_rows=20_000, timeout=120.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="report counts but don't write to Neo4j")
    parser.add_argument("--force", action="store_true",
                        help="rescore every extracted Document, including ones "
                             "already at the current score version")
    args = parser.parse_args()

    docs = load_unscored(args.force)
    scope = ("forced rescore" if args.force
             else f"unscored or below score version {SCORE_VERSION}")
    print(f"documents to score: {len(docs):,} ({scope})")
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
            SET d.extraction_score = row.score,
                d.extraction_score_version = $version
            """,
            {"rows": batch, "version": SCORE_VERSION},
        )
        written += len(batch)
        print(f"  wrote {written:,}/{len(rows):,}", end="\r")

    keyed = stamp_key_scores([r["filename"] for r in rows], chunk=WRITE_CHUNK)
    print(f"\nDone. Backfilled extraction_score on {written:,} document(s), "
          f"extraction_key_score on {keyed:,}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
