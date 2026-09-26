"""Clear possession types read off Canara Bank's conditional boilerplate.

Canara prints "For the properties which are in symbolic possession of the bank,
the Auction purchaser has to comply with …" near the end of its notices
whatever the possession actually is — two notices state "the Physical
Possession of which has been taken" and print it anyway. It is a condition,
not a statement about any lot, but it names "Symbolic Possession" and "physical
possession", and a lot's excerpt carries the notice's tail, so reads took it as
the answer for some lots of a notice and not others (pipeline/gap_fill now
blanks it out before reading: mask_possession_boilerplate).

For every notice carrying the block, a possession type the notice never
commits to anywhere else (pipeline.gap_fill.stated_possession_kinds — the
block and the unchosen "Symbolic / Constructive / Physical" menu both removed)
is dropped from the entity that holds it:

* the dropped values are recorded on the Document as
  ``possession_cleared_json`` ([{id, lot, value, cls, rule, at}]) so the change
  can be reversed;
* in a notice that states no type at all, the lot is marked "not in the notice"
  automatically (``by: "auto"``, rule ``boilerplate_only``) — a reviewer can
  undo it on the review page like any automatic mark;
* in a notice that states another type, the lot is left missing for a read;
* the lot's ``POSSESSION_IS`` edge is removed and ``possession_stated`` set
  false, since that edge was written from the dropped value.

A notice carrying any reviewer correction, or reviewed as verified / edited, is
skipped and listed: a person has been over it.

Run:
    NEO4J_HTTP_API=1 python -m scripts.clear_boilerplate_possession            # dry run
    NEO4J_HTTP_API=1 python -m scripts.clear_boilerplate_possession --apply
    NEO4J_HTTP_API=1 python -m scripts.clear_boilerplate_possession --apply --exclude-file busy.txt
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from api.neo4j_client import run_query, run_read_query
from pipeline.absence import AUTO, RULE_BOILERPLATE_ONLY
from pipeline.gap_fill import unsupported_possession
from pipeline.key_entities import absent_key, key_marks, stamp_key_scores

CLAUSE = "which are in symbolic possession of the bank"

SELECT = """
MATCH (d:Document)
WHERE d.extraction_json IS NOT NULL AND d.stitched_into IS NULL
  AND toLower(coalesce(d.stitched_markdown, d.markdown, '')) CONTAINS $clause
RETURN d.filename AS filename,
       coalesce(d.stitched_markdown, d.markdown) AS md,
       d.extraction_json AS ej,
       coalesce(d.extraction_corrections_json, '{}') AS cj,
       coalesce(d.extraction_review_status, 'pending') AS status,
       coalesce(d.possession_cleared_json, '[]') AS prior
ORDER BY d.filename
"""

WRITE_DOC = """
MATCH (d:Document {filename: $fn})
SET d.extraction_json = $ej,
    d.extraction_corrections_json = $cj,
    d.possession_cleared_json = $cleared
"""

CLEAR_LOT_EDGES = """
UNWIND $lots AS li
MATCH (d:Document {filename: $fn})-[:HAS_LOT]->(l:Lot)
WHERE toString(l.lot_index) = li
OPTIONAL MATCH (l)-[r:POSSESSION_IS]->(:PossessionType)
DELETE r
SET l.possession_stated = false,
    l.possession_cleared_rule = $rule
"""


def _loads(s, default):
    try:
        v = json.loads(s or "")
    except (TypeError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


def has_person(corrections: dict) -> bool:
    """True when any correction or mark was made by a person."""
    return any(not (isinstance(v, dict) and v.get("by") == AUTO)
               for v in corrections.values())


def plan_doc(row: dict) -> dict:
    """What to change on one Document — pure, so it is testable offline.

    Returns ``{"skip": reason}`` or ``{"ents", "corrections", "cleared",
    "lots", "absent"}`` (``cleared`` empty when there is nothing to do)."""
    corr = _loads(row.get("cj"), {})
    if row.get("status") in ("verified", "edited") or has_person(corr):
        return {"skip": "reviewed by a person"}
    ents = _loads(row.get("ej"), [])
    kept, cleared, absent = unsupported_possession(ents, row.get("md") or "")
    if not cleared:
        return {"cleared": []}
    held = key_marks(corr)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for lot in sorted(absent):
        mark = held.get((lot, "possession_type"))
        if mark and mark.get("by") != AUTO:
            continue
        corr[absent_key(lot, "possession_type")] = {
            "by": AUTO, "rule": RULE_BOILERPLATE_ONLY, "at": now}
    stamped = [{**c, "rule": RULE_BOILERPLATE_ONLY, "at": now} for c in cleared]
    return {"ents": kept, "corrections": corr, "cleared": stamped,
            "lots": sorted({c["lot"] for c in cleared}), "absent": sorted(absent)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--only", action="append", default=[], metavar="FILENAME")
    ap.add_argument("--exclude-file", default=None,
                    help="skip the filenames listed here, one per line — e.g. "
                         "notices a re-extraction is rewriting right now")
    args = ap.parse_args(argv)

    exclude: set[str] = set()
    if args.exclude_file:
        with open(args.exclude_file, encoding="utf-8") as fh:
            exclude = {ln.strip() for ln in fh if ln.strip()}
    rows = run_read_query(SELECT, {"clause": CLAUSE}, max_rows=20_000, timeout=180.0)
    if args.only:
        rows = [r for r in rows if r["filename"] in set(args.only)]
    print(f"{len(rows)} notice(s) carry the boilerplate"
          + (f"; excluding {len(exclude & {r['filename'] for r in rows})}" if exclude else "")
          + ("" if args.apply else "; dry run"))

    changed = lots = absent = 0
    skipped: list[str] = []
    for r in rows:
        fn = r["filename"]
        if fn in exclude:
            continue
        p = plan_doc(r)
        if "skip" in p:
            skipped.append(fn)
            continue
        if not p["cleared"]:
            continue
        changed += 1
        lots += len(p["lots"])
        absent += len(p["absent"])
        vals = ", ".join(f"lot {c['lot']}: {c['value']}" for c in p["cleared"])
        print(f"  {fn}: clear {vals}"
              + (f"; not in notice: lots {', '.join(p['absent'])}" if p["absent"] else
                 "; left missing (notice states another type)"))
        if not args.apply:
            continue
        prior = _loads(r.get("prior"), [])
        run_query(WRITE_DOC, {
            "fn": fn,
            "ej": json.dumps(p["ents"], ensure_ascii=False),
            "cj": json.dumps(p["corrections"], ensure_ascii=False),
            "cleared": json.dumps(prior + p["cleared"], ensure_ascii=False)})
        run_query(CLEAR_LOT_EDGES, {"fn": fn, "lots": p["lots"],
                                    "rule": RULE_BOILERPLATE_ONLY})
        stamp_key_scores([fn])

    print(f"{'changed' if args.apply else 'would change'} {changed} notice(s), "
          f"{lots} lot(s); {absent} marked not in the notice")
    if skipped:
        print(f"skipped {len(skipped)} reviewed by a person: {', '.join(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
