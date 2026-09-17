"""
reset_bulk_verifications.py
---------------------------
Undo a bulk extraction-verify: return swept documents to 'pending' so
`extraction_review_status = 'verified'` means what it claims again.

WHY
---
`POST /review/extraction/bulk-confirm` (api/review/extraction.py
`bulk_verify_extractions`) marks every document matching a filter as verified in
one statement. That is a legitimate queue-clearing tool, but the rows it writes
are indistinguishable afterwards from ones a human actually read — and two
things downstream treat 'verified' as human ground truth:

  * `evals/export_review_gold.py` snapshots verified extractions into the eval
    gold set, which future prompt and model changes are gated against. Exporting
    a bulk-verified corpus freezes today's errors in as the definition of
    correct, and every later "regression" is then measured against them.
  * a verified-only promotion gate (docs/SCHEMA.md, Provenance) would read as a
    quality gate while gating on nothing.

HOW A SWEEP IS IDENTIFIED
-------------------------
Not by count, and not by a hardcoded date: by timestamp identity. Every row a
single Cypher statement writes shares one `datetime()` to the millisecond, while
a human clicking verify N times leaves N distinct timestamps. So any group of
verified documents sharing an identical `extraction_verified_at`, larger than
`--min-cluster`, is a sweep. That signature keeps this script useful the next
time rather than making it a one-shot, and it cannot mistake a diligent review
session for a sweep no matter how fast the reviewer was.

WHAT IS NEVER TOUCHED
---------------------
A document carrying reviewer corrections (`extraction_corrections_json`) is left
verified whatever its timestamp says: someone edited its fields, so a human
demonstrably read it. Individually-verified rows (their own timestamp, or a
cluster at or below `--min-cluster`) are left alone too.

HISTORY IS MOVED, NOT ERASED
----------------------------
The sweep is real history and stays queryable — the old stamp moves to
`extraction_bulk_verified_at` / `_by` and `extraction_bulk_verify_reset_at`
records the undo. Only the claim that a person verified the document is
withdrawn. That also makes this reversible.

Idempotent: a second run finds nothing, because reset rows are no longer
'verified'.

Run (Bolt is often blocked outside the API host; NEO4J_HTTP_API=1 uses the
Aura Query API over HTTPS instead):

    NEO4J_HTTP_API=1 python -m scripts.reset_bulk_verifications --dry-run
    NEO4J_HTTP_API=1 python -m scripts.reset_bulk_verifications --apply
    NEO4J_HTTP_API=1 python -m scripts.reset_bulk_verifications --apply --at 2026-08-20T17:44:04.639Z
"""

from __future__ import annotations

import argparse

from api.neo4j_client import run_query, run_read_query

# A cluster this size or smaller is treated as hand review, not a sweep. One
# statement writing 25 identical millisecond stamps is already implausible as
# human clicking, so this is conservative by a wide margin.
DEFAULT_MIN_CLUSTER = 25

WRITE_CHUNK = 200


def find_clusters(rows: list[dict], min_cluster: int) -> tuple[list[dict], list[dict]]:
    """Split verified-row groups into (sweeps, kept).

    ``rows`` is one dict per distinct ``extraction_verified_at``:
    ``{at, by, n, with_corrections}``. Pure so the rule that decides what gets
    rewritten is testable without a database.
    """
    sweeps, kept = [], []
    for r in rows:
        n = int(r.get("n") or 0)
        # Corrections anywhere in the group mean a human edited fields there, so
        # the group is not a blind sweep. Conservative on purpose: this keeps a
        # whole cluster rather than trying to split it.
        if int(r.get("with_corrections") or 0) > 0 or n <= min_cluster:
            kept.append(r)
        else:
            sweeps.append(r)
    return sweeps, kept


def load_groups() -> list[dict]:
    return run_read_query(
        """
        MATCH (d:Document)
        WHERE d.extraction_json IS NOT NULL
          AND coalesce(d.extraction_review_status,'pending') = 'verified'
        RETURN toString(d.extraction_verified_at) AS at,
               d.extraction_verified_by            AS by,
               count(*)                            AS n,
               sum(CASE WHEN coalesce(d.extraction_corrections_json,'{}') <> '{}'
                        THEN 1 ELSE 0 END)         AS with_corrections
        ORDER BY n DESC
        """,
        {}, max_rows=5000, timeout=60.0,
    )


def filenames_at(at: str) -> list[str]:
    rows = run_read_query(
        """
        MATCH (d:Document)
        WHERE d.extraction_json IS NOT NULL
          AND coalesce(d.extraction_review_status,'pending') = 'verified'
          AND toString(d.extraction_verified_at) = $at
          AND coalesce(d.extraction_corrections_json,'{}') = '{}'
        RETURN d.filename AS filename
        ORDER BY d.filename
        """,
        {"at": at}, max_rows=20000, timeout=60.0,
    )
    return [r["filename"] for r in rows]


def reset(filenames: list[str]) -> int:
    """Return the batch to 'pending', moving the sweep's stamp aside."""
    done = 0
    for i in range(0, len(filenames), WRITE_CHUNK):
        chunk = filenames[i:i + WRITE_CHUNK]
        run_query(
            """
            UNWIND $fns AS fn
            MATCH (d:Document {filename: fn})
            WHERE coalesce(d.extraction_review_status,'pending') = 'verified'
            SET d.extraction_review_status       = 'pending',
                d.extraction_bulk_verified_at    = d.extraction_verified_at,
                d.extraction_bulk_verified_by    = d.extraction_verified_by,
                d.extraction_bulk_verify_reset_at = datetime()
            REMOVE d.extraction_verified_by, d.extraction_verified_at
            """,
            {"fns": chunk},
        )
        done += len(chunk)
        print(f"    reset {done}/{len(filenames)}")
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="report the clusters and what would change; write nothing")
    g.add_argument("--apply", action="store_true", help="perform the reset")
    ap.add_argument("--min-cluster", type=int, default=DEFAULT_MIN_CLUSTER,
                    help=f"groups larger than this are sweeps (default {DEFAULT_MIN_CLUSTER})")
    ap.add_argument("--at", action="append", default=None,
                    help="only this exact verified_at timestamp; repeatable")
    args = ap.parse_args()

    groups = load_groups()
    if not groups:
        print("No verified extractions found — nothing to do.")
        return

    sweeps, kept = find_clusters(groups, args.min_cluster)
    if args.at:
        wanted = set(args.at)
        skipped = [s for s in sweeps if s["at"] not in wanted]
        sweeps = [s for s in sweeps if s["at"] in wanted]
        kept = kept + skipped

    total_verified = sum(int(g["n"]) for g in groups)
    to_reset = sum(int(s["n"]) - int(s["with_corrections"] or 0) for s in sweeps)

    print(f"verified extractions: {total_verified} across {len(groups)} timestamp(s)\n")
    print("BULK SWEEPS (will be reset to pending):"
          if sweeps else "BULK SWEEPS: none")
    for s in sweeps:
        print(f"  {int(s['n']):6}  {s['at']}  by={s['by']}")
    print("\nKEPT AS VERIFIED:" if kept else "\nKEPT AS VERIFIED: none")
    for k in kept:
        why = ("has corrections" if int(k.get("with_corrections") or 0) > 0
               else f"cluster <= --min-cluster ({args.min_cluster})")
        print(f"  {int(k['n']):6}  {k['at']}  by={k['by']}  — {why}")

    print(f"\nwould reset: {to_reset}    would keep verified: {total_verified - to_reset}")
    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return
    if not sweeps:
        print("\nNothing to reset.")
        return

    print()
    done = 0
    for s in sweeps:
        fns = filenames_at(s["at"])
        print(f"  {s['at']}: {len(fns)} document(s)")
        done += reset(fns)
    print(f"\nreset {done} document(s) to pending.")
    print("extraction_review_status = 'verified' now means a human verified it.")


if __name__ == "__main__":
    main()
