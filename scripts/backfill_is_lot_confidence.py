"""Stamp `confidence` onto every `IS_LOT` edge already in the graph.

The two writers (`pipeline/apply_extractions.py::write_lot_matches` and
`scripts/resolve_lots.py`) set it from now on; the edges written before the
grade existed carry `method` and nothing else. This is the one-off that
catches them up.

The grade is derived, not decided here — `pipeline.match_confidence` is the
only table, so a backfilled edge and a freshly written one agree by
construction. An edge whose `method` is unrecognised (or missing entirely,
from some writer predating both) grades as UNKNOWN and is reported, never
guessed upward.

Idempotent: re-running rewrites the same values. Safe to run against a graph
mid-pipeline — it only ever sets a property, never creates or deletes an edge.

Run:  NEO4J_HTTP_API=1 python -m scripts.backfill_is_lot_confidence --dry-run
      NEO4J_HTTP_API=1 python -m scripts.backfill_is_lot_confidence
"""
from __future__ import annotations

import argparse

from dotenv import load_dotenv

from api.neo4j_client import run_query, run_read_query
from pipeline.match_confidence import MATCH_CONFIDENCE, UNKNOWN, confidence_for

load_dotenv()


def current_distribution() -> list[dict]:
    """`(method, confidence, count)` over every IS_LOT edge, commonest first."""
    return run_read_query("""
        MATCH ()-[r:IS_LOT]->()
        RETURN coalesce(r.method, '(none)') AS method,
               coalesce(r.confidence, '(unset)') AS confidence,
               count(*) AS edges
        ORDER BY edges DESC
    """, max_rows=200, timeout=60.0)


def backfill(*, dry_run: bool) -> dict[str, int]:
    """Set `confidence` per `method`. Returns {confidence: edges written}."""
    # One statement per grade rather than per edge: the graph holds ~3k edges
    # across ~10 methods, so this is a handful of round trips instead of
    # thousands. Grouping by grade (not by method) keeps it to three.
    by_grade: dict[str, list[str]] = {}
    for method, grade in MATCH_CONFIDENCE.items():
        by_grade.setdefault(grade, []).append(method)

    written: dict[str, int] = {}
    for grade, methods in sorted(by_grade.items()):
        res = run_read_query if dry_run else run_query
        cypher = ("MATCH ()-[r:IS_LOT]->() WHERE r.method IN $methods "
                  + ("" if dry_run else "SET r.confidence = $grade ")
                  + "RETURN count(r) AS n")
        rows = res(cypher, {"methods": methods, "grade": grade})
        written[grade] = int(rows[0]["n"]) if rows else 0

    # Anything the table does not know. Graded explicitly rather than left
    # null, so a consumer filtering on `confidence` sees it instead of
    # silently missing it — and so a later run can find it again.
    unknown_cypher = ("MATCH ()-[r:IS_LOT]->() "
                      "WHERE r.method IS NULL OR NOT r.method IN $known "
                      + ("" if dry_run else "SET r.confidence = $grade ")
                      + "RETURN count(r) AS n")
    res = run_read_query if dry_run else run_query
    rows = res(unknown_cypher,
               {"known": list(MATCH_CONFIDENCE), "grade": UNKNOWN})
    written[UNKNOWN] = int(rows[0]["n"]) if rows else 0
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="count what would be written, change nothing")
    args = ap.parse_args()

    print("Before:")
    total_before = 0
    for row in current_distribution():
        total_before += row["edges"]
        print(f"  {row['method']:<22} {row['confidence']:<12} {row['edges']:>6}")
    print(f"  {'TOTAL':<22} {'':<12} {total_before:>6}")

    written = backfill(dry_run=args.dry_run)
    verb = "would set" if args.dry_run else "set"
    print(f"\n{verb}:")
    for grade, n in sorted(written.items(), key=lambda kv: -kv[1]):
        print(f"  {grade:<12} {n:>6}")
    print(f"  {'TOTAL':<12} {sum(written.values()):>6}")

    if sum(written.values()) != total_before:
        print(f"\n  WARNING: graded {sum(written.values())} of {total_before} "
              f"edges — the two should agree; a mismatch means an edge was "
              f"matched by no branch above.")

    if not args.dry_run:
        print("\nAfter:")
        for row in current_distribution():
            print(f"  {row['method']:<22} {row['confidence']:<12} "
                  f"{row['edges']:>6}")
        ungraded = run_read_query(
            "MATCH ()-[r:IS_LOT]->() WHERE r.confidence IS NULL "
            "RETURN count(r) AS n")
        n = int(ungraded[0]["n"]) if ungraded else 0
        print(f"\nedges still ungraded: {n}"
              + ("  <-- should be 0" if n else "  OK"))


if __name__ == "__main__":
    main()
