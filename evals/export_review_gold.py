"""Export reviewer-verified extractions from Neo4j into the eval gold set.

Closes the human-in-the-loop: when a reviewer Verifies a notice in the
extraction-review UI (api/review/extraction.py sets extraction_review_status =
'verified', with any per-field corrections), this script snapshots that verified
extraction as a gold case. The verified output then becomes a regression anchor —
future prompt/model changes are gated against what a human signed off on.

For each verified Document it:
  1. applies the reviewer's text corrections to the stored entities,
  2. flattens them with the SAME logic the live eval uses
     (evals.langextract_eval.flatten_records),
  3. writes the notice markdown to evals/fixtures/<aid>.txt, and
  4. emits a gold entry to evals/langextract_gold_reviewed.json.

evals/langextract_eval.py loads that file alongside the hand-labelled seed.

Run:  NEO4J_HTTP_API=1 python -m evals.export_review_gold
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from api.neo4j_client import run_read_query
from evals.langextract_eval import flatten_records

FIX = Path(__file__).resolve().parent / "fixtures"
OUT = Path(__file__).resolve().parent / "langextract_gold_reviewed.json"

_GOLD_SCALARS = [
    "legal_basis", "bank_name", "assignor_bank", "trust_name", "court_reference",
    "possession_type", "village", "taluk", "district", "registration_district",
    "registration_sub_district", "borrower_primary",
]


def _safe(aid: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]", "_", str(aid))[:120]


def _records_from_stored(extraction_json: str, corrections_json: str) -> list[dict]:
    """Stored entities with the reviewer's text corrections applied."""
    try:
        ents = json.loads(extraction_json or "[]")
    except json.JSONDecodeError:
        ents = []
    try:
        corr = json.loads(corrections_json or "{}")
    except json.JSONDecodeError:
        corr = {}
    out = []
    for i, e in enumerate(ents):
        fid = e.get("id") or str(i)
        text = (corr.get(fid) or {}).get("value", e.get("text", ""))
        out.append({"cls": e.get("cls"), "text": text, "attrs": e.get("attrs") or {}})
    return out


def _gold_fields(flat: dict) -> tuple[dict, dict]:
    fields = {k: flat[k] for k in _GOLD_SCALARS if flat.get(k)}
    if flat.get("reserve_set"):
        fields["reserve_price_num"] = int(sorted(flat["reserve_set"])[0])
    if flat.get("emd_set"):
        fields["emd_num"] = int(sorted(flat["emd_set"])[0])
    identifiers = {k: sorted(v)[0] for k, v in flat["identifiers"].items() if v}
    return fields, identifiers


def fetch_verified() -> list[dict]:
    return run_read_query(
        """
        MATCH (d:Document)
        WHERE d.extraction_review_status = 'verified'
          AND d.extraction_json IS NOT NULL
        OPTIONAL MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d)
        WITH d, collect(a.auction_id) AS aids
        RETURN coalesce(aids[0], d.filename)                AS aid,
               d.notice_type                                AS notice_type,
               d.markdown                                   AS md,
               d.extraction_json                            AS ej,
               coalesce(d.extraction_corrections_json,'{}') AS cj,
               d.extraction_verified_by                     AS by,
               toString(d.extraction_verified_at)           AS at
        """,
        max_rows=20_000, timeout=120.0)


def run() -> int:
    FIX.mkdir(parents=True, exist_ok=True)
    gold = []
    for r in fetch_verified():
        aid = _safe(r["aid"])
        flat = flatten_records(_records_from_stored(r["ej"], r["cj"]))
        fields, identifiers = _gold_fields(flat)
        if not fields:
            print(f"  skip {aid}: no scorable fields")
            continue
        (FIX / f"{aid}.txt").write_text(r["md"] or "", encoding="utf-8")
        gold.append({
            "aid": aid,
            "notice_type": r.get("notice_type") or "single",
            "fields": fields,
            "identifiers": identifiers,
            "source": "review",
            "verified_by": r.get("by"),
            "verified_at": r.get("at"),
        })
        print(f"  {aid}: {len(fields)} fields, {len(identifiers)} identifiers")
    OUT.write_text(json.dumps(gold, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {len(gold)} reviewer-verified gold cases -> {OUT.name}")
    print("re-gate with:  python -m evals.langextract_eval")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
