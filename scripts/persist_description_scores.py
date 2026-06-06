#!/usr/bin/env python3
"""Persist the LLM completeness-judge outputs into Neo4j.

Reads the JSONL written by ``scripts/score_descriptions.py`` and writes the
judge verdict onto each :AuctionProperty so the review API/UI can sort and
display it. This is the "persist scores" step — it does NOT auto-verify
anything (no ``description_verified`` writes).

Fields written (per the completeness-scoring design spec):
  a.description_completeness      -> judge.completeness   (graded 0..1)
  a.description_complete          -> judge.complete       (bool)
  a.description_missing_parts     -> judge.missing_parts  (list[str])
  a.description_wrong_property    -> judge.wrong_property  (bool)
  a.description_judge_confidence  -> judge.confidence      (0..1)
  a.description_judge_reasoning   -> judge.reasoning       (str)
  a.description_text_overlap      -> text_overlap          (cheap pre-filter)
  a.description_scored_at         -> now (audit)

Only rows the judge scored successfully (``ok == true``) are written; failed
rows are skipped and reported so they can be re-scored first.

    NEO4J_HTTP_API=1 python -m scripts.persist_description_scores
    NEO4J_HTTP_API=1 python -m scripts.persist_description_scores --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from api.neo4j_client import run_query

IN = Path("output/description_judge.jsonl")
WRITE_CHUNK = 200


def load_rows() -> tuple[list[dict], int]:
    """Return (writable_rows, skipped_failures)."""
    if not IN.exists():
        raise SystemExit(f"missing {IN} — run scripts.score_descriptions first")
    rows, skipped = [], 0
    for line in IN.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not r.get("ok"):
            skipped += 1
            continue
        mp = r.get("missing_parts")
        if not isinstance(mp, list):
            mp = []
        rows.append({
            "auction_id":    r["auction_id"],
            "completeness":  r.get("completeness"),
            "complete":      r.get("complete"),
            "missing_parts": [str(x) for x in mp],
            "wrong_property": bool(r.get("wrong_property")),
            "confidence":    r.get("confidence"),
            "reasoning":     r.get("reasoning"),
            "text_overlap":  r.get("text_overlap"),
        })
    return rows, skipped


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def persist(rows: list[dict]) -> int:
    now_iso = datetime.now(timezone.utc).isoformat()
    written = 0
    for batch in chunked(rows, WRITE_CHUNK):
        result = run_query("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.auction_id})
            SET a.description_completeness     = row.completeness,
                a.description_complete         = row.complete,
                a.description_missing_parts    = row.missing_parts,
                a.description_wrong_property   = row.wrong_property,
                a.description_judge_confidence = row.confidence,
                a.description_judge_reasoning  = row.reasoning,
                a.description_text_overlap     = row.text_overlap,
                a.description_scored_at        = datetime($at)
            RETURN a.auction_id AS aid
        """, {"rows": batch, "at": now_iso})
        written += len(result) if result else 0
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be written without touching Neo4j")
    args = ap.parse_args()

    rows, skipped = load_rows()
    print(f"loaded {len(rows)} scored rows ({skipped} failed-judge rows skipped)")
    if args.dry_run:
        wp = sum(1 for r in rows if r["wrong_property"])
        comp = sum(1 for r in rows if (r["completeness"] or 0) >= 1.0)
        print(f"  dry-run: would write {len(rows)} rows "
              f"(wrong_property={wp}, completeness==1.0={comp})")
        return 0
    if not rows:
        print("nothing to persist.")
        return 0
    written = persist(rows)
    print(f"persisted {written} rows to Neo4j (matched on auction_id)")
    if written != len(rows):
        print(f"  WARNING: {len(rows) - written} rows had no matching "
              f"AuctionProperty and were not written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
