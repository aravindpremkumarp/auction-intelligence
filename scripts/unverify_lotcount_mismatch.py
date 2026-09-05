"""Return classification-verified notices whose lot count contradicts their
label to the classification review queue.

READ-ONLY unless --apply is passed.

The contradiction
-----------------
`Document.notice_type` is the cluster count — how many :AuctionProperty rows
link to the notice — corrected by human review, and `pipeline/extract_routing.
select_extract_model` routes on it alone. That count is SCOPE-FILTERED: lots
outside Tamil Nadu, or never scraped, are simply absent from it. So a notice
selling eight properties of which one was scraped counts as one, is labelled
'single', and is asked a single-lot question by the model chosen for the easy
case.

Promotion then writes what the notice actually says. 32 documents now carry 2+
:Lot nodes under a 'single' label, and they score 64.8 on average against 88.0
for notices that really do sell one property. `KARNTK17819389935603.jpg` is the
sharp end: 28,510 characters, 49 lot markers in the source, labelled 'single',
6 lots extracted, score 10.

What this does, and what it deliberately does not
-------------------------------------------------
It REMOVES `notice_type_verified_at` / `_by`, which is the only thing the
review queue's `pending` filter reads (`api/review/queries.py:320`). The
document reappears for a human to classify.

It does NOT touch `notice_type` itself. The lot count says the label is wrong;
it does not say what the label should be, and a notice whose extraction
over-split is a real possibility (`lot_under_recall` cuts the other way too).
Setting the value is the reviewer's call, and doing it here would re-verify by
another name.

It does NOT touch `notice_type_overridden` (5 of the 32 carry it) or
`notice_type_review_notes`. Both are the record of what a human previously
decided; a re-review should see that history, not a cleaned-up version of it.

It does NOT stamp `extraction_stale_at`. That marker means "the prompt this
extraction was built on has changed", and nothing has changed yet — the
reviewer's own save stamps it when they alter the label, which is the loop
`api/review/queries.py:601` already closes.

Run:  NEO4J_HTTP_API=1 python -m scripts.unverify_lotcount_mismatch [--apply]
"""
from __future__ import annotations

import argparse

from api.neo4j_client import run_query, run_read_query

#: Documents whose promoted lot count contradicts a non-'multi' label. The
#: coalesce mirrors `select_extract_model`, which treats any label that is not
#: 'multi' — including a missing one — as single.
_MISMATCH = """
MATCH (d:Document)-[:HAS_LOT]->(l:Lot)
WITH d, count(l) AS lots
WHERE coalesce(d.notice_type, 'single') <> 'multi'
  AND lots > 1
  AND d.notice_type_verified_at IS NOT NULL
"""


def find() -> list[dict]:
    return run_read_query(
        _MISMATCH +
        "RETURN d.filename AS filename, d.notice_type AS notice_type, "
        "       lots AS promoted_lots, "
        "       d.expected_lot_count AS expected_lot_count, "
        "       d.extraction_score AS score, "
        "       d.notice_type_overridden AS overridden, "
        "       toString(d.notice_type_verified_at) AS verified_at "
        "ORDER BY d.extraction_score",
        max_rows=5_000, timeout=120.0)


def unverify() -> int:
    rows = run_query(
        _MISMATCH +
        "REMOVE d.notice_type_verified_at, d.notice_type_verified_by "
        "RETURN d.filename AS filename")
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="remove the verification (default: report only)")
    ap.add_argument("--show", type=int, default=10,
                    help="how many rows to print (default 10)")
    args = ap.parse_args()

    rows = find()
    print(f"{len(rows)} verified document(s) whose promoted lot count "
          f"contradicts a non-'multi' label")
    for r in rows[:args.show]:
        print(f"  {r['filename']}  label={r['notice_type']} "
              f"expected={r['expected_lot_count']} "
              f"promoted_lots={r['promoted_lots']} "
              f"score={r['score']} "
              f"{'(human-overridden)' if r.get('overridden') else ''}")
    if len(rows) > args.show:
        print(f"  … {len(rows) - args.show} more")
    if not args.apply:
        print("REPORT ONLY — pass --apply to return these to the queue")
        return 0
    n = unverify()
    print(f"unverified {n} document(s) — they are now 'pending' in the "
          f"classification review queue")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
