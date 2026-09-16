"""
score_vs_review.py
------------------
Does extraction_score predict what a reviewer actually does?

The 0-100 score in pipeline/validators.py is a hand-weighted sum of penalties
(critical=30, high=20, med=10, low=4) that nobody has ever checked against a
human verdict. This report is that check, and it must run BEFORE any weight is
re-tuned: without it, a weight change is one guess replacing another.

The reviewer's own record is the label (api/review/extraction.py):
  extraction_review_status      'pending' | 'edited' | 'verified'
  extraction_corrections_json   {field_id: {value, by, at, notes}}

A reviewed notice is one a person has ruled on — 'edited' or 'verified'. Two
outcomes are read off it:
  edited          the reviewer changed at least one field (the coarse verdict)
  correction rate corrections / fields extracted (how much they changed)

A score worth trusting separates those: low scores should be edited far more
often than high ones. Read the report as:

  * monotone edit rate across buckets, wide spread  -> the score ranks quality,
    and the review queue's score filter is doing real work.
  * flat edit rate                                  -> the score is noise for
    triage; the auto-verify path in api/review/extraction.py is verifying
    documents nobody has any evidence about.
  * high scores edited as often as low ones         -> the score measures
    completeness while reviewers are catching wrongness. Different quantity.
    Fix the checks, not the weights.

Read-only: no writes, no LLM call, safe to run against production.

Scores computed on different validators.py penalties are different scales, so by
default only Documents at the current SCORE_VERSION are counted (run
scripts/backfill_extraction_scores.py first to re-level the rest).
--all-versions pools them anyway, for a look at a corpus mid-migration; it
weakens every number in the report.

Run:
    python -m scripts.score_vs_review
    python -m scripts.score_vs_review --bucket 20 --min-reviewed 50
    python -m scripts.score_vs_review --all-versions
"""

from __future__ import annotations

import argparse
import json

from api.neo4j_client import run_read_query
from pipeline.validators import SCORE_VERSION

REVIEWED = ("edited", "verified")


def load_reviewed(all_versions: bool) -> list[dict]:
    """Every scored Document a person has ruled on, with its correction count."""
    where = [
        "d.extraction_score IS NOT NULL",
        "d.extraction_json IS NOT NULL",
        "coalesce(d.extraction_review_status, 'pending') IN $reviewed",
    ]
    if not all_versions:
        where.append("coalesce(d.extraction_score_version, 0) = $version")
    return run_read_query(
        "MATCH (d:Document) WHERE " + " AND ".join(where) + " "
        "RETURN d.filename AS filename, d.extraction_score AS score, "
        "       coalesce(d.extraction_review_status, 'pending') AS status, "
        "       coalesce(d.extraction_corrections_json, '{}') AS corrections, "
        "       d.extraction_json AS ej",
        {"reviewed": list(REVIEWED), "version": SCORE_VERSION},
        max_rows=50_000, timeout=120.0)


def _count(raw: str) -> int:
    try:
        v = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return 0
    return len(v) if isinstance(v, (dict, list)) else 0


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation, ties averaged. Pure Python — no scipy in requirements.

    Rank, not Pearson on the raw values: the score saturates at 0 (five 'high'
    issues floor a document), so its distribution has a lump at the bottom that
    a linear correlation reads as signal. Ranks only ask whether the ORDER
    agrees, which is all the score claims to give.
    """
    n = len(xs)
    if n < 3:
        return None

    def ranks(vs: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: vs[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vs[order[j + 1]] == vs[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return None if dx == 0 or dy == 0 else num / (dx * dy)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bucket", type=int, default=10,
                   help="score bucket width (default 10)")
    p.add_argument("--min-reviewed", type=int, default=30,
                   help="warn when fewer reviewed documents than this (default 30)")
    p.add_argument("--all-versions", action="store_true",
                   help="pool scores from older validators.py penalties too")
    args = p.parse_args()
    width = max(1, min(100, args.bucket))

    docs = load_reviewed(args.all_versions)
    scope = ("all score versions" if args.all_versions
             else f"score version {SCORE_VERSION}")
    print(f"reviewed documents: {len(docs):,} ({scope})")
    if not docs:
        print("nothing to report — no reviewed documents at this score version.\n"
              "run scripts/backfill_extraction_scores.py, or pass --all-versions.")
        return 0
    if len(docs) < args.min_reviewed:
        print(f"  [warn] under {args.min_reviewed} reviewed documents — every "
              f"rate below is noise. Review more before re-tuning weights.")

    rows = []
    for d in docs:
        n_fields = _count(d["ej"])
        n_corr = _count(d["corrections"])
        rows.append({
            "score": float(d["score"]),
            "edited": 1.0 if (d["status"] == "edited" or n_corr) else 0.0,
            "rate": (n_corr / n_fields) if n_fields else 0.0,
            "fields": n_fields,
            "corrections": n_corr,
        })

    # A perfect 100 belongs in the top bucket, not one of its own: 100 // width
    # would open an extra bucket holding only that score.
    top = 99 // width
    buckets: dict[int, list[dict]] = {}
    for r in rows:
        buckets.setdefault(min(int(r["score"]) // width, top), []).append(r)

    print(f"\n  {'score':>9}  {'n':>6}  {'edited':>8}  {'corr/100 fields':>16}")
    for b in sorted(buckets):
        lo = b * width
        hi = 100 if b == top else lo + width - 1
        rs = buckets[b]
        edited = 100 * sum(r["edited"] for r in rs) / len(rs)
        fields = sum(r["fields"] for r in rs)
        corr = 100 * sum(r["corrections"] for r in rs) / fields if fields else 0
        print(f"  {lo:>3}-{hi:<5}  {len(rs):>6}  {edited:>7.1f}%  {corr:>16.2f}")

    rho_edit = _spearman([r["score"] for r in rows], [r["edited"] for r in rows])
    rho_rate = _spearman([r["score"] for r in rows], [r["rate"] for r in rows])
    print("\nSpearman (score vs outcome) — negative is what we want: a higher "
          "score should mean less reviewer work")
    for label, rho in (("edited", rho_edit), ("correction rate", rho_rate)):
        if rho is None:
            print(f"  {label:>15}: n/a (too few documents, or no variation)")
        else:
            strength = ("no signal" if abs(rho) < 0.1 else
                        "weak" if abs(rho) < 0.3 else
                        "moderate" if abs(rho) < 0.5 else "strong")
            print(f"  {label:>15}: {rho:+.3f}  ({strength})")

    floored = sum(1 for r in rows if r["score"] == 0)
    if floored:
        print(f"\n  [note] {floored:,}/{len(rows):,} reviewed documents scored 0 — "
              "the penalty sum saturates there, so they are unordered among "
              "themselves and every correlation above is computed on a tie.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
