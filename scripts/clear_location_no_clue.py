"""Clear the "not in the notice" marks a keyword check put on locations.

Until now the gap-filler marked a lot's location — or its missing village or
taluk — "not in the notice" without a read whenever the lot's text lacked a
word like "village" or "taluk" (rule ``no_clue``). But a village is named
without that word: "situated within the limits of Alagapuram Pudur, Salem
Taluk". Those marks are sticky — every later gap-fill run skips them — so the
places they hide were never looked for. pipeline/absence no longer applies the
keyword rule to locations; this removes the marks it already wrote, so the
next ``scripts.fill_gaps --keys location`` reads those lots.

Only automatic ``no_clue`` location marks go (``by: "auto"``). A person's mark,
and an automatic mark made after two reads found nothing (``not_found``), stay.

Run:
    NEO4J_HTTP_API=1 python -m scripts.clear_location_no_clue            # dry run
    NEO4J_HTTP_API=1 python -m scripts.clear_location_no_clue --apply
"""
from __future__ import annotations

import argparse
import json
import sys

from api.neo4j_client import run_query, run_read_query
from pipeline.absence import AUTO, RULE_NO_CLUE

SELECT = """
MATCH (d:Document)
WHERE d.extraction_corrections_json CONTAINS ':location'
  AND d.extraction_corrections_json CONTAINS $rule
OPTIONAL MATCH (d)-[:HAS_LOT]->(l:Lot)
RETURN d.filename AS filename,
       d.extraction_corrections_json AS cj,
       collect([toString(l.lot_index), l.place_status]) AS lots
ORDER BY d.filename
"""

WRITE = """
MATCH (d:Document {filename: $fn})
WHERE d.extraction_corrections_json = $old
SET d.extraction_corrections_json = $cj
RETURN count(d) AS n
"""


def is_location_no_clue(key: str, mark) -> bool:
    return (str(key).startswith("absent:") and str(key).endswith(":location")
            and isinstance(mark, dict) and mark.get("by") == AUTO
            and mark.get("rule") == RULE_NO_CLUE)


def plan_doc(cj: str | None) -> tuple[dict, list[str]]:
    """``(kept corrections, lot indexes cleared)`` — pure, testable offline."""
    try:
        corr = json.loads(cj or "{}")
    except (TypeError, ValueError):
        return {}, []
    if not isinstance(corr, dict):
        return {}, []
    gone = [k for k, v in corr.items() if is_location_no_clue(k, v)]
    kept = {k: v for k, v in corr.items() if k not in gone}
    return kept, sorted((k.split(":", 2)[1] for k in gone),
                        key=lambda s: (len(s), s))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--quiet", action="store_true", help="totals only, no per-notice lines")
    args = ap.parse_args(argv)

    rows = run_read_query(SELECT, {"rule": f'"{RULE_NO_CLUE}"'},
                          max_rows=20_000, timeout=180.0)
    notices = lots = unplaced = 0
    changed: list[str] = []
    for r in rows:
        kept, cleared = plan_doc(r["cj"])
        if not cleared:
            continue
        notices += 1
        lots += len(cleared)
        status = {li: st for li, st in (r.get("lots") or []) if li is not None}
        unplaced += sum(status.get(li) not in (None, "resolved") for li in cleared)
        if not args.quiet:
            print(f"  {r['filename']}: lots {', '.join(cleared)} — place "
                  + ", ".join(status.get(li) or "no lot" for li in cleared))
        if args.apply:
            n = run_query(WRITE, {"fn": r["filename"], "old": r["cj"],
                                  "cj": json.dumps(kept, ensure_ascii=False)})
            if n and n[0].get("n"):
                changed.append(r["filename"])
            else:
                print(f"  ! {r['filename']} changed since it was read — skipped")

    print(f"{'cleared' if args.apply else 'would clear'} {lots} location mark(s) "
          f"on {notices} notice(s); {unplaced} of those lots are not yet placed")
    if args.apply and changed:
        from pipeline.key_entities import stamp_key_scores
        stamp_key_scores(changed)
        print("next: python -m scripts.fill_gaps --keys location --unplaced "
              "--dry-run --limit 20")
    return 0


if __name__ == "__main__":
    sys.exit(main())
