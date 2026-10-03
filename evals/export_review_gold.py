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
  3. for a multi-lot notice, also emits the per-lot truth (``lots``: reserve,
     EMD, location parts and identifiers per lot_index) that
     evals.langextract_eval.score_multi grades lot binding against,
  4. turns a person's "not in the notice" mark on possession into EXPECT_NULL
     (spelled evals.langextract_eval.EXPECT_NULL_JSON), so an invented value
     counts as wrong,
  5. records the verified full_description span per lot
     (``description_spans``) so the eval can measure span overlap,
  6. writes the notice markdown to evals/fixtures/<aid>.txt, and
  7. emits a gold entry to evals/langextract_gold_reviewed.json, tagged with
     the strata evals/gold_manifest.json knows about.

evals/langextract_eval.py loads that file alongside the hand-labelled seed.

Run:  NEO4J_HTTP_API=1 python -m evals.export_review_gold
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from api.neo4j_client import run_read_query
from evals.langextract_eval import (EXPECT_NULL_JSON, GOLD_MANIFEST,
                                    flatten_records, group_by_lot)
from pipeline.key_entities import AUTO, key_marks

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
    """Stored entities with the reviewer's corrections applied — text fixes
    and the entities the reviewer added (``add:*`` keys), via the same overlay
    promotion uses, so the gold set and the graph agree on what a document
    says."""
    from pipeline.apply_extractions import entities_with_corrections
    return [{"cls": e.get("cls"), "text": e.get("text", ""),
             "start": e.get("start"), "end": e.get("end"),
             "attrs": e.get("attrs") or {}}
            for e in entities_with_corrections(extraction_json, corrections_json)]


_LOT_LOC = ("village", "taluk", "district", "registration_district",
            "registration_sub_district")


def _gold_lots(records: list[dict]) -> list[dict]:
    """Per-lot truth for a multi notice, in the shape langextract_gold uses:
    one dict per lot carrying a reserve price, with its EMD, location parts
    and identifiers. Lots without a reserve price are not auction units and
    are left out, the same rule score_multi applies to extractions."""
    lots = []
    for grp in group_by_lot(records):
        if not grp["reserves"]:
            continue
        lot: dict = {"reserve_price_num": int(sorted(grp["reserves"])[0])}
        if grp["emds"]:
            lot["emd_num"] = int(sorted(grp["emds"])[0])
        for key, setk in (("village", "villages"), ("taluk", "taluks"),
                          ("district", "districts"),
                          ("registration_district", "reg_districts"),
                          ("registration_sub_district", "reg_subs")):
            if grp[setk]:
                lot[key] = sorted(grp[setk])[0]
        ids = {k: sorted(v)[0] for k, v in grp["identifiers"].items() if v}
        if ids:
            lot["identifiers"] = ids
        lots.append(lot)
    return lots


def _person_absent(corrections_json: str) -> set[tuple[str, str]]:
    """{(lot, key)} a PERSON marked "not in the notice" (automatic marks are
    the pipeline's own guess and never make gold)."""
    try:
        corr = json.loads(corrections_json or "{}")
    except json.JSONDecodeError:
        return set()
    return {at for at, m in key_marks(corr).items()
            if m.get("kind") == "absent" and m.get("by") != AUTO}


def _description_spans(records: list[dict]) -> dict[str, list[int]]:
    """{lot_index: [start, end]} of the verified full_description per lot."""
    out: dict[str, list[int]] = {}
    for r in records:
        if r.get("cls") != "full_description" or r.get("start") is None:
            continue
        li = str((r.get("attrs") or {}).get("lot_index") or "1")
        out.setdefault(li, [int(r["start"]), int(r["end"])])
    return out


def _strata(aid: str, notice_type: str, records: list[dict], md: str) -> list[str]:
    """Strata from the manifest when it names the notice, else the cheap
    ones that can be read off the text itself."""
    try:
        known = json.loads(GOLD_MANIFEST.read_text(encoding="utf-8")).get("notices", {})
    except (OSError, json.JSONDecodeError):
        known = {}
    if aid in known and known[aid].get("strata"):
        return list(known[aid]["strata"])
    out = [notice_type]
    if "<table" in (md or ""):
        out.append("html_table")
    if re.search(r"[\u0B80-\u0BFF]", md or ""):
        out.append("tamil")
    n_lots = sum(1 for g in group_by_lot(records) if g["reserves"])
    if n_lots >= 40:
        out.append("40plus_lots")
    if any((r.get("attrs") or {}).get("undivided_share") or
           "undivided" in (r.get("text") or "").lower()
           for r in records if r.get("cls") == "extent"):
        out.append("apartment_uds")
    return out


def _gold_fields(flat: dict, *, multi: bool = False,
                 person_absent: set[tuple[str, str]] = frozenset()) -> tuple[dict, dict]:
    """Notice-level gold fields (+ identifiers for a single notice).

    On a multi notice the per-lot keys stay out of ``fields`` (they live in
    ``lots``); on a single notice reserve/EMD ride along as before. A person's
    "not in the notice" on lot 1's possession becomes EXPECT_NULL."""
    per_lot = {"village", "taluk", "district", "registration_district",
               "registration_sub_district", "borrower_primary"}
    keys = [k for k in _GOLD_SCALARS if not (multi and k in per_lot)]
    fields = {k: flat[k] for k in keys if flat.get(k)}
    if ("1", "possession_type") in person_absent and not fields.get("possession_type"):
        fields["possession_type"] = EXPECT_NULL_JSON
    if not multi:
        if flat.get("reserve_set"):
            fields["reserve_price_num"] = int(sorted(flat["reserve_set"])[0])
        if flat.get("emd_set"):
            fields["emd_num"] = int(sorted(flat["emd_set"])[0])
    identifiers = ({} if multi else
                   {k: sorted(v)[0] for k, v in flat["identifiers"].items() if v})
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
        records = _records_from_stored(r["ej"], r["cj"])
        notice_type = r.get("notice_type") or "single"
        lots = _gold_lots(records) if notice_type == "multi" else []
        multi = bool(lots)
        flat = flatten_records(records)
        fields, identifiers = _gold_fields(
            flat, multi=multi, person_absent=_person_absent(r["cj"]))
        if not fields and not lots:
            print(f"  skip {aid}: no scorable fields")
            continue
        (FIX / f"{aid}.txt").write_text(r["md"] or "", encoding="utf-8")
        entry = {
            "aid": aid,
            "notice_type": notice_type,
            "fields": fields,
            "identifiers": identifiers,
            "source": "review",
            "verified_by": r.get("by"),
            "verified_at": r.get("at"),
            "strata": _strata(aid, notice_type, records, r["md"] or ""),
        }
        if multi:
            entry["lots"] = lots
        spans = _description_spans(records)
        if spans:
            entry["description_spans"] = spans
        gold.append(entry)
        print(f"  {aid}: {len(fields)} fields, {len(identifiers)} identifiers, "
              f"{len(lots)} lots")
    OUT.write_text(json.dumps(gold, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {len(gold)} reviewer-verified gold cases -> {OUT.name}")
    print("re-gate with:  python -m evals.langextract_eval")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
