"""
scripts/resolve_branches.py
---------------------------
Give every notice's listing branch one identity — within its bank.

Branch names are word soup around a place or unit name: Canara Bank's Trichy
recovery office arrives as ``ARM Branch Trichy``, ``Trichy ARM Branch``,
``ARM TRICHY`` and three more. The per-bank token-set rule
(:func:`pipeline.entity_resolution.branch_key`) absorbs those; anything only
similarity can see becomes a review proposal, exactly like lenders.

Two identity sources were measured and rejected before settling on names:

* **IFSC** — notices print the account-servicing IFSC, not the listing
  branch's: one ICICI code covers Nagercoil, Theni and Trichy. Not a key.
* **The scraped ``(:Branch)`` nodes** — keyed by name alone, so one "Chennai"
  node is claimed by 23 different banks. Not identities.

So identity is (bank, branch) from the notice, and everything is scoped per
bank: keys, merges, proposals and verdicts.

Writes::

    d.branch_canonical    the agreed spelling, within d's bank
    d.branch_name_raw     what the notice said, for audit
    d.branch_resolved_at  when this last ran
    d.branch_attention    true while d's branch sits in an open proposal

Proposals go to ``(:PipelineState {key: 'branch_resolution'})`` and surface
in the resolution review queue; verdicts are ``branch-merge`` decisions the
next run applies before proposing anything.

Usage:
    python -m scripts.resolve_branches --dry-run
    python -m scripts.resolve_branches

Auth: NEO4J_URI/USERNAME/PASSWORD(/DATABASE).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict

from pipeline.entity_resolution import propose_merges, resolve
from pipeline.resolution_review import (
    apply_branch_merges, filter_branch_proposals,
)
from scripts.resolution_decisions import load_decisions
from scripts.score_ink_coverage import nq

# Branch names are shorter than lender names, so scores run higher on less
# evidence; 90 keeps the queue to pairs worth a human's time.
REVIEW_MIN_SCORE = 90.0


def collect() -> tuple[dict[str, Counter], dict[str, tuple[str, str]]]:
    """Return (bank -> branch name counts, file_path -> (bank, raw branch)).

    Scoped by ``d.bank_canonical`` — the lender resolver's output — so the
    same branch string under two banks never meets itself. One branch per
    notice, counted once per Document for the same reason lenders were.
    """
    rows = nq("""
        MATCH (d:Document)
        WHERE d.extraction_json IS NOT NULL AND d.bank_canonical IS NOT NULL
        RETURN d.file_path, d.bank_canonical, d.extraction_json
    """)
    per_bank: dict[str, Counter] = defaultdict(Counter)
    per_doc: dict[str, tuple[str, str]] = {}
    for file_path, bank, ej in rows:
        try:
            entities = json.loads(ej or "[]")
        except (TypeError, ValueError):
            continue
        for e in entities:
            name = ((e.get("attrs") or {}).get("branch") or "").strip()
            if name:
                per_bank[bank][name] += 1
                per_doc[file_path] = (bank, name)
                break
    return per_bank, per_doc


def write_back(per_doc: dict[str, tuple[str, str]],
               by_value: dict[tuple[str, str], str],
               open_pairs: set[tuple[str, str]]) -> int:
    rows = []
    for file_path, (bank, raw) in per_doc.items():
        canonical = by_value.get((bank, raw), raw)
        rows.append({
            "file_path": file_path, "raw": raw, "canonical": canonical,
            "attention": (bank, canonical) in open_pairs,
        })
    for i in range(0, len(rows), 500):
        nq("""
            UNWIND $rows AS row
            MATCH (d:Document {file_path: row.file_path})
            SET d.branch_name_raw    = row.raw,
                d.branch_canonical   = row.canonical,
                d.branch_attention   = CASE WHEN row.attention
                                            THEN true ELSE NULL END,
                d.branch_resolved_at = datetime()
        """, {"rows": rows[i:i + 500]})
    return len(rows)


def write_state(n_banks: int, n_raw: int, n_groups: int, merged: int,
                proposals: list[dict]) -> None:
    nq("""
        MERGE (s:PipelineState {key: 'branch_resolution'})
        SET s.updated_at       = datetime(),
            s.banks            = $banks,
            s.raw_values       = $raw,
            s.entities         = $entities,
            s.merged_spellings = $merged,
            s.proposals_json   = $proposals,
            s.proposals_open   = $n_proposals
    """, {
        "banks": n_banks, "raw": n_raw, "entities": n_groups,
        "merged": merged,
        "proposals": json.dumps(proposals, ensure_ascii=False),
        "n_proposals": len(proposals),
    })


def run(*, dry_run: bool = False,
        min_score: float = REVIEW_MIN_SCORE) -> dict:
    """One full resolution pass; the CLI and the API's apply button both land
    here. Returns a summary the caller can store or print."""
    per_bank, per_doc = collect()
    decisions = load_decisions()

    by_value: dict[tuple[str, str], str] = {}
    proposals: list[dict] = []
    n_groups = merged = by_decision = 0
    for bank, counts in per_bank.items():
        res = apply_branch_merges(
            resolve(counts, kind="branch"), decisions, bank=bank)
        n_groups += len(res["groups"])
        merged += sum(g["merged"] for g in res["groups"])
        by_decision += sum(g.get("merged_by_decision", 0)
                           for g in res["groups"])
        for raw, label in res["by_value"].items():
            by_value[(bank, raw)] = label
        for p in propose_merges(res["groups"], min_score=min_score):
            proposals.append({**p, "bank": bank})
    proposals = filter_branch_proposals(proposals, decisions)
    proposals.sort(key=lambda p: -p["score"])

    n_raw = sum(len(c) for c in per_bank.values())
    print(f"{n_raw} (bank, branch) string(s) across {len(per_doc)} notice(s) "
          f"and {len(per_bank)} bank(s) -> {n_groups} branch(es); "
          f"{merged} spelling(s) absorbed"
          + (f" ({by_decision} by human decision)" if by_decision else ""))
    print(f"\n{len(proposals)} pair(s) for human review at >= {min_score}:")
    for p in proposals[:12]:
        print(f"  {p['score']:5.1f}  {p['a'][:34]:<36} vs  {p['b'][:34]:<36} "
              f"[{p['bank'][:24]}]")

    summary = {"notices": len(per_doc), "banks": len(per_bank),
               "branches": n_groups, "merged": merged,
               "proposals_open": len(proposals)}
    if dry_run:
        print("\n[dry-run] nothing written")
        return summary

    open_pairs = {(p["bank"], p["a"]) for p in proposals} \
        | {(p["bank"], p["b"]) for p in proposals}
    wrote = write_back(per_doc, by_value, open_pairs)
    write_state(len(per_bank), n_raw, n_groups, merged, proposals)
    print(f"\nResolved {wrote} notice(s); {len(proposals)} proposal(s) stored "
          f"for review")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report only")
    ap.add_argument("--min-score", type=float, default=REVIEW_MIN_SCORE)
    args = ap.parse_args()
    run(dry_run=args.dry_run, min_score=args.min_score)
    return 0


if __name__ == "__main__":
    sys.exit(main())
