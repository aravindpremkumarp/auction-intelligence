"""Regenerate evals/fixtures/notice_context.json from the graph.

The eval must send the model exactly what production sends it — the reviewer's
lot count and the notice's portal listings (see
pipeline.langextract_examples.prompt_description_for). Those live in Neo4j, but
an eval that queried Neo4j live would stop being reproducible: it could not
score the same thing twice, and it could not run offline at all. So the context
is snapshotted into a fixture and checked in, and this script refreshes it.

Run it after the gold set gains notices, or after a batch of classification
review changes the lot counts:

    NEO4J_HTTP_API=1 python -m evals.refresh_notice_context
    NEO4J_HTTP_API=1 python -m evals.refresh_notice_context --dry-run

Auth: NEO4J_URI/USERNAME/PASSWORD(/DATABASE).
"""
from __future__ import annotations

import argparse
import json
import sys

from evals.langextract_eval import NOTICE_CONTEXT, load_gold
from scripts.score_ink_coverage import nq

# One row per portal listing on the notice, matching pipeline.load_extractions
# .ROSTER_CYPHER — the eval's context has to be built the same way the
# pipeline's is, or the eval measures a prompt production never sends.
QUERY = """
UNWIND $aids AS aid
MATCH (a0:AuctionProperty {auction_id: aid})-[:HAS_DOCUMENT]->(d:Document)
OPTIONAL MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d)
WITH aid, d, [row IN collect(DISTINCT {
       aid: a.auction_id,
       reserve: a.reserve_price_num, emd: a.emd_num,
       village: a.village, district: a.district,
       area: a.total_area, ptype: a.property_type_norm,
       borrower: head([(a)-[:HAS_BORROWER]->(bo) | bo.name]),
       desc: a.website_description})
     WHERE row.reserve IS NOT NULL OR row.emd IS NOT NULL
        OR row.village IS NOT NULL] AS roster
RETURN aid, d.filename, d.expected_lot_count, roster
ORDER BY aid
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be written, write nothing")
    args = ap.parse_args(argv)

    gold = load_gold()
    aids = sorted({g["aid"] for g in gold})
    rows = nq(QUERY, {"aids": aids})
    ctx = {aid: {"filename": fn, "expected_lot_count": elc, "roster": roster}
           for aid, fn, elc, roster in rows}

    by_type = {g["aid"]: g.get("notice_type") for g in gold}
    for aid in aids:
        c = ctx.get(aid)
        if c is None:
            print(f"  {aid}  MISSING from graph — eval falls back to no context")
            continue
        elc, n = c["expected_lot_count"], len(c["roster"])
        flag = ""
        # A 'single' in gold that the review gate says holds several lots (or
        # vice versa) means one of the two is wrong. The eval can still run —
        # it just tells you the gold and the graph disagree about this notice.
        if by_type.get(aid) == "single" and (elc or 1) > 1:
            flag = "  <-- gold says single, review gate says multi"
        elif by_type.get(aid) == "multi" and (elc or 0) == 1:
            flag = "  <-- gold says multi, review gate says single"
        print(f"  {aid}  lots={elc}  roster={n}{flag}")

    if args.dry_run:
        print(f"\n[dry-run] {len(ctx)}/{len(aids)} notices; nothing written")
        return 0
    NOTICE_CONTEXT.parent.mkdir(parents=True, exist_ok=True)
    NOTICE_CONTEXT.write_text(
        json.dumps(ctx, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(f"\nwrote {NOTICE_CONTEXT} — {len(ctx)}/{len(aids)} notices")
    return 0


if __name__ == "__main__":
    sys.exit(main())
