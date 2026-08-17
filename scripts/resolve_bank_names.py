"""
scripts/resolve_bank_names.py
-----------------------------
Give every notice's lender one identity.

LangExtract stores the bank name as the notice printed it, so the corpus holds
199 distinct ``bank_name`` strings for roughly 130 institutions. This script
groups them with :mod:`pipeline.entity_resolution` and writes the resolved name
back to each Document:

    d.bank_canonical      the agreed spelling for this lender
    d.bank_name_raw       exactly what the notice said, kept for audit
    d.entity_resolved_at  when resolution last ran (the funnel's predicate)

Only the safe rule writes: normalized token-set equality, which merges case and
legal-form differences and nothing else. Names that merely *look* alike are
written to a review list instead, on a single ``(:PipelineState {key:
'entity_resolution'})`` node, because similarity cannot tell
``Asset Reconstruction Company (India) Limited`` from ``India SME Asset
Reconstruction Company Limited`` — two different companies scoring 92.9.

Usage:
    python -m scripts.resolve_bank_names --dry-run     # group + preview
    python -m scripts.resolve_bank_names               # write
Options: --min-score 88

Auth: NEO4J_URI/USERNAME/PASSWORD(/DATABASE).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

from pipeline.entity_resolution import REVIEW_MIN_SCORE, propose_merges, resolve
from pipeline.resolution_review import apply_bank_merges, filter_proposals
from scripts.resolution_decisions import load_decisions
from scripts.score_ink_coverage import nq


def collect() -> tuple[Counter, dict[str, str]]:
    """Return (name -> notice count, file_path -> raw name).

    A notice names its lender on many entities; counting once per Document
    keeps a 30-lot notice from outvoting thirty single-lot ones.
    """
    rows = nq("""
        MATCH (d:Document)
        WHERE d.extraction_json IS NOT NULL
        RETURN d.file_path, d.extraction_json
    """)
    counts: Counter = Counter()
    per_doc: dict[str, str] = {}
    for file_path, ej in rows:
        try:
            entities = json.loads(ej or "[]")
        except (TypeError, ValueError):
            continue
        for e in entities:
            name = ((e.get("attrs") or {}).get("bank_name") or "").strip()
            if name:
                counts[name] += 1
                per_doc[file_path] = name
                break        # first named lender wins; one per notice
    return counts, per_doc


def write_back(per_doc: dict[str, str], by_value: dict[str, str],
               proposals: list[dict]) -> int:
    """Write each notice's resolved lender, plus its attention flag.

    ``bank_attention`` marks a document whose lender sits in a still-open
    lookalike pair — the reason it cannot count as review-complete yet. It is
    recomputed on every run, so deciding a pair and re-running clears it.
    """
    open_names = {p["a"] for p in proposals} | {p["b"] for p in proposals}
    rows = [{"file_path": fp, "raw": raw,
             "canonical": by_value.get(raw, raw),
             "attention": by_value.get(raw, raw) in open_names}
            for fp, raw in per_doc.items()]
    for i in range(0, len(rows), 500):
        nq("""
            UNWIND $rows AS row
            MATCH (d:Document {file_path: row.file_path})
            SET d.bank_name_raw      = row.raw,
                d.bank_canonical     = row.canonical,
                d.bank_attention     = CASE WHEN row.attention
                                            THEN true ELSE NULL END,
                d.entity_resolved_at = datetime()
        """, {"rows": rows[i:i + 500]})
    return len(rows)


def write_state(groups: list[dict], proposals: list[dict],
                raw_count: int) -> None:
    """Persist the corpus-level summary + the review list.

    One singleton node rather than a node per proposal: these are advisory
    pairs a human works through, not graph entities, and keeping them together
    means the dashboard reads one row.
    """
    nq("""
        MERGE (s:PipelineState {key: 'entity_resolution'})
        SET s.updated_at      = datetime(),
            s.raw_values      = $raw,
            s.entities        = $entities,
            s.merged_spellings = $merged,
            s.proposals_json  = $proposals,
            s.proposals_open  = $n_proposals
    """, {
        "raw": raw_count,
        "entities": len(groups),
        "merged": sum(g["merged"] for g in groups),
        "proposals": json.dumps(proposals, ensure_ascii=False),
        "n_proposals": len(proposals),
    })


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="group + preview only")
    ap.add_argument("--min-score", type=float, default=REVIEW_MIN_SCORE,
                    help="fuzzy score at which a pair is queued for review")
    args = ap.parse_args()

    counts, per_doc = collect()
    # Human verdicts come first: approved merges apply before anything is
    # proposed, and pairs already ruled on never reappear in the queue.
    decisions = load_decisions()
    res = apply_bank_merges(resolve(counts), decisions)
    groups = res["groups"]
    merged = sum(g["merged"] for g in groups)
    by_decision = sum(g.get("merged_by_decision", 0) for g in groups)
    print(f"{len(counts)} distinct name(s) across {len(per_doc)} notice(s) "
          f"-> {len(groups)} lender(s); {merged} spelling(s) absorbed"
          + (f" ({by_decision} by human decision)" if by_decision else ""))

    print("\ntop lenders after resolution:")
    for g in groups[:12]:
        extra = f"   (+{g['merged']} spelling{'s' if g['merged'] != 1 else ''})" \
            if g["merged"] else ""
        print(f"  {g['count']:>4}  {g['canonical'][:52]}{extra}")

    proposals = filter_proposals(
        propose_merges(groups, min_score=args.min_score), decisions)
    print(f"\n{len(proposals)} pair(s) for human review at >= {args.min_score}:")
    for p in proposals[:12]:
        print(f"  {p['score']:5.1f}  {p['a'][:40]:<42} ({p['a_count']})"
              f"  vs  {p['b'][:40]} ({p['b_count']})")

    if args.dry_run:
        print("\n[dry-run] nothing written")
        return 0

    wrote = write_back(per_doc, res["by_value"], proposals)
    write_state(groups, proposals, len(counts))
    print(f"\nResolved {wrote} notice(s); {len(proposals)} proposal(s) stored "
          f"for review")
    return 0


if __name__ == "__main__":
    sys.exit(main())
