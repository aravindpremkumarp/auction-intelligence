"""
reground_extractions.py
-----------------------
Puts source spans back on entities already stored without one.

Every extraction carries where it came from (``start``/``end`` into the
document's markdown). LangExtract leaves those None when the model's answer is
not byte-identical to the page, and some of those answers are the page's own
words with the whitespace or case changed — text it really does hold, filed as
if it did not. ``api.review.grounding.ground_missing`` decides what can honestly
be placed — the same rule the write path now applies — and this walks the corpus
applying it to everything extracted before that.

A small cleanup, not a fix for the corpus's grounding rate: on the live corpus
it placed 90 of 5,689 unplaced entities across 19 of 3,162 documents. The rest
were composed by the model and are not on the page in any form. What makes them
affordable is the scoring change that shipped with this — see
pipeline/validators.py on charging a missing span once instead of twice.

No LLM call and no re-extraction: the entities, their classes and their
attributes are untouched, so this is free to run and safe to re-run. Only
``start``/``end`` are ever written, and only from None.

A span feeds the reviewer's highlight and the validators' full_description
containment check, so run the score backfill afterwards — a regrounded document
can score differently on the same scale:

    python -m scripts.reground_extractions --dry-run
    python -m scripts.reground_extractions
    python -m scripts.backfill_extraction_scores

A follower page of a stitched notice (stitched_into) is skipped for the reason
it is never extracted on its own: its text rides in the leader's
stitched_markdown, so there is nothing here to ground against.
"""

from __future__ import annotations

import argparse
import json

from api.neo4j_client import run_query, run_read_query
from api.review.grounding import ground_missing

WRITE_CHUNK = 100

LOAD_CYPHER = """
MATCH (d:Document)
WHERE d.extraction_json IS NOT NULL AND d.stitched_into IS NULL
RETURN d.filename AS filename,
       coalesce(d.stitched_markdown, d.markdown) AS md,
       d.extraction_json AS ej
ORDER BY d.filename
"""

WRITE_CYPHER = """
UNWIND $rows AS row
MATCH (d:Document {filename: row.filename})
SET d.extraction_json = row.ej,
    d.extraction_regrounded_at = datetime()
"""


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be regrounded, write nothing")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after this many documents (0 = all)")
    args = parser.parse_args()

    docs = run_read_query(LOAD_CYPHER, None, max_rows=20_000, timeout=180.0)
    if args.limit:
        docs = docs[:args.limit]
    print(f"documents with an extraction: {len(docs):,}")

    rows = []
    unparseable = 0
    scanned = ungrounded_before = recovered = 0
    for d in docs:
        try:
            ents = json.loads(d["ej"] or "[]")
        except json.JSONDecodeError:
            unparseable += 1
            continue
        scanned += len(ents)
        before = sum(1 for e in ents if e.get("start") is None)
        if not before:
            continue
        ungrounded_before += before
        fixed = ground_missing(ents, d["md"] or "")
        if fixed:
            recovered += fixed
            rows.append({"filename": d["filename"],
                         "ej": json.dumps(ents, ensure_ascii=False)})

    if unparseable:
        print(f"  [skip] {unparseable} document(s) had unparseable extraction_json")
    pct = (100 * recovered / ungrounded_before) if ungrounded_before else 0
    print(f"entities: {scanned:,} | ungrounded: {ungrounded_before:,} | "
          f"regrounded: {recovered:,} ({pct:.1f}% of them), "
          f"across {len(rows):,} document(s)")

    if args.dry_run:
        print("\n(dry-run) no writes performed.")
        return 0
    if not rows:
        print("nothing to write.")
        return 0

    written = 0
    for batch in chunked(rows, WRITE_CHUNK):
        run_query(WRITE_CYPHER, {"rows": batch})
        written += len(batch)
        print(f"  wrote {written:,}/{len(rows):,}", end="\r")
    print(f"\nDone. Regrounded {recovered:,} entities in {written:,} document(s).")
    print("Next: python -m scripts.backfill_extraction_scores")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
