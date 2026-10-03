"""Queue notices for the next gold-set version, stratified by failure mode.

The eval is only as honest as its gold, and nine hand-labelled notices cannot
tell a 2-point regression from noise. This script lists the notices worth a
reviewer's time next, so gold v2 (100) and v3 (500) grow from the places the
reader actually fails rather than from whatever is on top of the queue:

  * spot-check claims a person graded ``wrong`` or ``not_in_source``
    (pipeline/spotcheck), by field;
  * corrections orphaned by a re-read (``orphaned:*`` keys, pipeline/extraction_ids);
  * key facts the reader marked CONTESTED / ILLEGIBLE or dropped
    (``extraction_issue_codes`` / ``extraction_dropped``);
  * strata the manifest is short of (evals/gold_manifest.json): tamil,
    stitched, 40+ lots, html tables, poor scans.

It writes evals/gold_candidates.json — {aid, filename, reasons, strata,
score} — and prints the strata counts against the manifest so the sprint can
be balanced. Nothing is verified here; a person does that in the extraction
review UI, and evals/export_review_gold.py promotes what they verify.

Run:  NEO4J_HTTP_API=1 python -m scripts.gold_candidates [--limit 60]
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

from api.neo4j_client import run_read_query

OUT = Path(__file__).resolve().parents[1] / "evals" / "gold_candidates.json"
MANIFEST = Path(__file__).resolve().parents[1] / "evals" / "gold_manifest.json"

# One row per notice with the signals the strata and reasons are read from.
QUERY = """
MATCH (d:Document)
WHERE d.extraction_json IS NOT NULL
OPTIONAL MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d)
WITH d, collect(a.auction_id)[0] AS aid
OPTIONAL MATCH (s:SpotCheckSample {filename: d.filename})
WHERE s.verdict IN ['wrong', 'not_in_source']
WITH d, aid, collect(DISTINCT s.field) AS wrong_fields
RETURN coalesce(aid, d.filename) AS aid, d.filename AS filename,
       d.notice_type AS notice_type, d.expected_lot_count AS lots,
       d.extraction_score AS score, d.ocr_health_score AS ocr,
       d.extraction_issue_codes AS issues, d.extraction_dropped AS dropped,
       d.extraction_corrections_json AS cj, d.stitched_markdown IS NOT NULL AS stitched,
       d.markdown CONTAINS '<table' AS has_table,
       d.markdown =~ '(?s).*[\\u0B80-\\u0BFF].*' AS tamil,
       d.extraction_review_status AS status, wrong_fields
"""


def strata_of(r: dict) -> list[str]:
    out = [r.get("notice_type") or "single"]
    if r.get("has_table"):
        out.append("html_table")
    if r.get("tamil"):
        out.append("tamil")
    if (r.get("lots") or 0) >= 40:
        out.append("40plus_lots")
    if r.get("stitched"):
        out.append("stitched")
    if (r.get("ocr") or 100) < 95:
        out.append("poor_scan")
    return out


def reasons_of(r: dict) -> list[str]:
    out = [f"spotcheck_wrong:{f}" for f in (r.get("wrong_fields") or [])]
    try:
        corr = json.loads(r.get("cj") or "{}")
    except json.JSONDecodeError:
        corr = {}
    if any(str(k).startswith("orphaned:") for k in corr):
        out.append("orphaned_corrections")
    for code in (r.get("issues") or []):
        if code in ("cross_field_inconsistent", "contested_key_fact",
                    "lot_under_recall", "full_description_incomplete"):
            out.append(code)
    if r.get("dropped"):
        out.append("dropped_key_facts")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=60)
    args = ap.parse_args(argv)
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {"notices": {}}
    have = collections.Counter(st for n in manifest.get("notices", {}).values()
                               for st in n.get("strata", []))
    cands = []
    for r in run_read_query(QUERY, {}, timeout=120.0, max_rows=100_000):
        if r["aid"] in manifest.get("notices", {}) or r.get("status") == "verified":
            continue
        reasons, strata = reasons_of(r), strata_of(r)
        rare = [s for s in strata if have[s] < 5]
        if not reasons and not rare:
            continue
        cands.append({"aid": r["aid"], "filename": r["filename"], "score": r["score"],
                      "lots": r.get("lots"), "ocr": r.get("ocr"),
                      "reasons": reasons, "strata": strata,
                      "priority": len(reasons) * 2 + len(rare)})
    cands.sort(key=lambda c: (-c["priority"], c["score"] or 0))
    cands = cands[:args.limit]
    OUT.write_text(json.dumps(cands, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    want = collections.Counter(st for c in cands for st in c["strata"])
    print(f"{len(cands)} candidates -> {OUT.name}")
    for st in sorted(set(have) | set(want)):
        print(f"  {st:22} in gold {have[st]:3}   queued {want[st]:3}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
