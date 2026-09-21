"""Per-lot completeness of a LangExtract run: recall first, then the four fields.

`pipeline/validators.py` scores a NOTICE — one flag per defect kind, deliberately
not a function of how many lots the notice holds (see its INVARIANT). That is the
right shape for the prompt-improvement loop and the wrong shape for the question
asked before and after a run: *of the lots that exist, how many did we get, and
how many of those are usable?* This module answers exactly that, counting LOTS,
not documents:

  1. expected vs extracted   Document.expected_lot_count (the reviewer/portal lot
                             count, stitched-aware) against the distinct
                             lot_index values the extraction emitted.
  2. no full_description     lots with no full_description block, or an empty one.
  3. no place                lots missing village / taluk / district (taluk is
                             satisfied by `hobli`, its Karnataka equivalent;
                             `registration_sub_district` is counted separately —
                             it is a registration office, NOT a revenue taluk).
  4. no extent               lots with no extent entity carrying an area.
  5. no property_type        lots with no property.property_type.

Everything here is pure except the two loaders. It reads what is already stored
(Document.extraction_json), so a report costs no LLM call and can be re-run as
often as wanted — before a run, as a baseline; after it, to see what moved.

Usage:
    NEO4J_HTTP_API=1 python -m pipeline.lot_completeness
    NEO4J_HTTP_API=1 python -m pipeline.lot_completeness --limit 50 --json out.json
    python -m pipeline.lot_completeness --jsonl docs.jsonl     # offline

The offline form reads one JSON object per line:
    {"aid": "...", "expected_lot_count": 3, "entities": [{cls, text, attrs}, ...]}

Auth (graph mode): NEO4J_URI/USERNAME/PASSWORD(/DATABASE).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: Classes that belong to a lot. A notice-level class (full_terms, emd_account,
#: contact) carries no lot_index, so counting lots off every class would invent
#: a lot 1 for a notice whose property blocks are all numbered 2..n.
LOT_CLASSES = frozenset({
    "property", "full_description", "location", "extent", "boundary",
    "identifier", "schedule", "auction_terms", "outstanding", "borrower",
})

#: An area is present when the extent entity states one of these (or says the
#: lot is an undivided share of a parent extent). An extent span with no number
#: in any attribute still counts through its verbatim text — the model often
#: leaves "2400 sq.ft." in the span and fills nothing.
_AREA_ATTRS = ("extent_sqft", "total_area", "super_built_up_area",
               "built_up_area", "carpet_area", "undivided_share")

#: The three place fields a lot is read by. `hobli` stands in for `taluk`
#: because Karnataka notices name one and never the other.
PLACE_FIELDS = ("village", "taluk", "district")
_PLACE_ALIASES = {"taluk": ("taluk", "hobli")}


def _filled(v) -> bool:
    """A value the extraction actually states. "null"/"n/a"/"" do not count."""
    if v is None:
        return False
    s = str(v).strip().lower()
    return s not in {"", "null", "none", "na", "n/a", "not stated", "unknown"}


def _as_dicts(entities) -> list[dict]:
    """Accept stored extraction_json dicts AND live LangExtract Extractions.

    Stored: {id, cls, text, start, end, attrs} (pipeline/load_extractions.py).
    Live:   objects with .extraction_class / .extraction_text / .attributes.
    """
    out: list[dict] = []
    for e in entities or []:
        if isinstance(e, dict):
            out.append({"cls": e.get("cls") or e.get("extraction_class"),
                        "text": e.get("text") or e.get("extraction_text") or "",
                        "attrs": e.get("attrs") or e.get("attributes") or {}})
        else:
            out.append({"cls": getattr(e, "extraction_class", None),
                        "text": getattr(e, "extraction_text", "") or "",
                        "attrs": getattr(e, "attributes", None) or {}})
    return out


def lot_rows(entities) -> dict[str, dict]:
    """One row per lot: what that lot has, keyed by lot_index as a string.

    str() on lot_index for the same reason pipeline/validators.py does it: a
    notice whose entities carry both 1 and "1" is ONE lot, not two.
    """
    rows: dict[str, dict] = {}
    for e in _as_dicts(entities):
        cls, attrs = e["cls"], e["attrs"]
        if cls not in LOT_CLASSES:
            continue
        li = str(attrs.get("lot_index") or "1")
        row = rows.setdefault(li, {
            "lot_index": li, "full_description": False, "extent": False,
            "property_type": False, "registration_sub_district": False,
            **{f: False for f in PLACE_FIELDS},
        })
        if cls == "full_description" and _filled(e["text"]):
            row["full_description"] = True
        elif cls == "extent" and (any(_filled(attrs.get(k)) for k in _AREA_ATTRS)
                                  or _filled(e["text"])):
            row["extent"] = True
        elif cls == "property" and _filled(attrs.get("property_type")):
            row["property_type"] = True
        # Place fields are declared on `location`, but a model that puts them on
        # the property block has still read them off the notice — the loader
        # folds those across too, so the report counts them the same way.
        for field in PLACE_FIELDS:
            if any(_filled(attrs.get(k)) for k in _PLACE_ALIASES.get(field, (field,))):
                row[field] = True
        if _filled(attrs.get("registration_sub_district")):
            row["registration_sub_district"] = True
    return rows


def doc_completeness(entities, expected_lot_count=None, aid: str = "") -> dict:
    """Per-document counts. `missing_*` are LOT counts within this document."""
    rows = lot_rows(entities)
    lots = list(rows.values())
    extracted = len(lots)
    expected = int(expected_lot_count) if expected_lot_count is not None else None
    missing_place = [r["lot_index"] for r in lots
                     if not all(r[f] for f in PLACE_FIELDS)]
    return {
        "aid": aid,
        "expected_lots": expected,
        "extracted_lots": extracted,
        # None when the notice has no confirmed count — an unknown is not a match.
        "lot_delta": None if expected is None else extracted - expected,
        "missing_full_description": [r["lot_index"] for r in lots
                                     if not r["full_description"]],
        "missing_place": missing_place,
        "missing_village": [r["lot_index"] for r in lots if not r["village"]],
        "missing_taluk": [r["lot_index"] for r in lots if not r["taluk"]],
        "missing_district": [r["lot_index"] for r in lots if not r["district"]],
        "missing_registration_sub_district": [
            r["lot_index"] for r in lots if not r["registration_sub_district"]],
        "missing_extent": [r["lot_index"] for r in lots if not r["extent"]],
        "missing_property_type": [r["lot_index"] for r in lots
                                  if not r["property_type"]],
    }


_LOT_GAPS = ("missing_full_description", "missing_place", "missing_village",
             "missing_taluk", "missing_district",
             "missing_registration_sub_district", "missing_extent",
             "missing_property_type")


def rollup(docs: list[dict]) -> dict:
    """Corpus totals. Recall is reported ONLY over documents with a known
    expected count — averaging an unknown as agreement would flatter the run."""
    known = [d for d in docs if d["expected_lots"] is not None]
    lots = sum(d["extracted_lots"] for d in docs)
    gaps = {k: sum(len(d[k]) for d in docs) for k in _LOT_GAPS}
    return {
        "documents": len(docs),
        # An extraction that emitted no lot at all — stored, scored, and empty.
        # It reads as "0 gaps" in every field count below, so it is called out
        # here or it hides.
        "documents_with_no_lots": sum(1 for d in docs if d["extracted_lots"] == 0),
        "documents_with_expected_count": len(known),
        "documents_without_expected_count": len(docs) - len(known),
        "expected_lots": sum(d["expected_lots"] for d in known),
        "extracted_lots_where_expected_known": sum(d["extracted_lots"] for d in known),
        "extracted_lots": lots,
        "documents_matching_expected": sum(1 for d in known if d["lot_delta"] == 0),
        "documents_under_expected": sum(1 for d in known if d["lot_delta"] < 0),
        "documents_over_expected": sum(1 for d in known if d["lot_delta"] > 0),
        "lots_short_of_expected": sum(-d["lot_delta"] for d in known
                                      if d["lot_delta"] < 0),
        "lots_beyond_expected": sum(d["lot_delta"] for d in known
                                    if d["lot_delta"] > 0),
        "lot_gaps": gaps,
        "lot_gaps_pct": {k: (round(v / lots * 100, 1) if lots else 0.0)
                         for k, v in gaps.items()},
    }


#: Gaps that put a document in the review queue. `missing_place` is left out
#: because it is the union of the three place fields and would double-count
#: them; `missing_registration_sub_district` because most notices legitimately
#: state no SRO — it is reported for completeness, not as a defect.
_QUEUE_GAPS = tuple(k for k in _LOT_GAPS
                    if k not in {"missing_place",
                                 "missing_registration_sub_district"})


def worst_documents(docs: list[dict], limit: int = 20) -> list[dict]:
    """Documents to look at first: biggest recall shortfall, then most gap-lots."""
    def key(d):
        short = -d["lot_delta"] if d["lot_delta"] is not None and d["lot_delta"] < 0 else 0
        return (short, sum(len(d[k]) for k in _QUEUE_GAPS))
    ranked = sorted((d for d in docs if key(d) != (0, 0)), key=key, reverse=True)
    return ranked[:limit]


# ── loaders ──────────────────────────────────────────────────────────────────

def load_from_graph(limit: int | None = None, filename: str | None = None) -> list[dict]:
    """Already-extracted notices from Neo4j. A stitched follower page is skipped
    (its text rides in the leader's stitched_markdown), and the expected count is
    the stitched one when the notice was stitched — same coalesce the extractor
    prompts with, so the two sides of the comparison agree."""
    from api.neo4j_client import run_read_query
    where = "d.extraction_json IS NOT NULL AND d.stitched_into IS NULL"
    params: dict = {}
    if filename:
        where += " AND d.filename = $fn"
        params["fn"] = filename
    rows = run_read_query(
        f"MATCH (d:Document) WHERE {where} "
        "RETURN coalesce(d.filename, d.storage_key) AS aid, "
        "       d.extraction_json AS ej, "
        "       coalesce(d.stitched_expected_lot_count, d.expected_lot_count) "
        "AS expected_lot_count ORDER BY aid"
        + (f" LIMIT {int(limit)}" if limit else ""),
        params or None, max_rows=20_000, timeout=120.0)
    out = []
    for r in rows:
        try:
            ents = json.loads(r["ej"] or "[]")
        except json.JSONDecodeError:
            ents = []
        out.append({"aid": r["aid"], "entities": ents,
                    "expected_lot_count": r["expected_lot_count"]})
    return out


def load_from_jsonl(path: str) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


# ── report ───────────────────────────────────────────────────────────────────

def build_report(rows: list[dict], worst: int = 20) -> dict:
    docs = [doc_completeness(r.get("entities"), r.get("expected_lot_count"),
                             aid=str(r.get("aid") or ""))
            for r in rows]
    return {"totals": rollup(docs), "worst": worst_documents(docs, worst),
            "documents": docs}


def print_report(report: dict) -> None:
    t = report["totals"]
    lots = t["extracted_lots"]
    print(f"documents                {t['documents']}  "
          f"(with a confirmed lot count: {t['documents_with_expected_count']}, "
          f"without: {t['documents_without_expected_count']})")
    print(f"  extracted no lot at all  {t['documents_with_no_lots']}")
    print("\n— recall (documents with a confirmed lot count) —")
    print(f"  expected lots          {t['expected_lots']}")
    print(f"  extracted lots         {t['extracted_lots_where_expected_known']}")
    print(f"  documents matching     {t['documents_matching_expected']}")
    print(f"  under / over           {t['documents_under_expected']} / "
          f"{t['documents_over_expected']}  "
          f"(lots short {t['lots_short_of_expected']}, "
          f"extra {t['lots_beyond_expected']})")
    print(f"\n— field gaps (of {lots} extracted lots) —")
    labels = {
        "missing_full_description": "no full_description",
        "missing_place": "no full village+taluk+district",
        "missing_village": "  no village",
        "missing_taluk": "  no taluk (or hobli)",
        "missing_district": "  no district",
        "missing_registration_sub_district": "  no registration sub-district",
        "missing_extent": "no extent",
        "missing_property_type": "no property_type",
    }
    for k, label in labels.items():
        print(f"  {label:<32} {t['lot_gaps'][k]:>6}  "
              f"({t['lot_gaps_pct'][k]}%)")
    if report["worst"]:
        print("\n— look at these first —")
        for d in report["worst"]:
            exp = "?" if d["expected_lots"] is None else d["expected_lots"]
            print(f"  {d['aid']}  lots {d['extracted_lots']}/{exp}  "
                  f"no_desc={len(d['missing_full_description'])} "
                  f"no_place={len(d['missing_place'])} "
                  f"no_extent={len(d['missing_extent'])} "
                  f"no_type={len(d['missing_property_type'])}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jsonl", help="offline: one {aid, expected_lot_count, "
                                    "entities} object per line")
    ap.add_argument("--filename", help="graph mode: one Document by filename")
    ap.add_argument("--limit", type=int, default=None, help="cap documents")
    ap.add_argument("--worst", type=int, default=20,
                    help="how many documents to list in the review queue")
    ap.add_argument("--json", dest="json_out", help="write the full report here")
    args = ap.parse_args()

    rows = (load_from_jsonl(args.jsonl) if args.jsonl
            else load_from_graph(args.limit, args.filename))
    if not rows:
        print("no extracted documents found")
        return 1
    report = build_report(rows, worst=args.worst)
    print_report(report)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
