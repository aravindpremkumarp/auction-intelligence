"""Stamp `notice_fields_lot` / `notice_fields_consensus` onto existing listings.

`pipeline/apply_extractions.py::write_fields` records them from now on. The
listings enriched before the split existed carry their notice fields with no
way to tell a value read off ONE confirmed lot from a value every lot on the
notice agreed to — which is the whole difference between "this listing's
village" and "a village true whichever lot this is".

Derived, not decided here. This re-runs the same pure functions the pipeline
uses — `group_lots`, `match_lots_to_listings`, `sole_claimants`,
`consensus_and_contested`, `field_provenance` — so a backfilled listing and a
freshly written one agree by construction. No field VALUE is written, read or
changed: the only writes are the two lists.

Two honesty guards, because this runs over values an EARLIER pass wrote:

* a name is kept only where the node actually holds that property now
  (``a[k] IS NOT NULL``), so the provenance never claims a field that some
  later script cleared or that this document never supplied;
* only listings the grounded pipeline enriched (`enrichment_source =
  'grounded_extraction'`) are touched at all.

What it cannot do is re-date the claim: it asserts where a value WOULD come
from if applied now, against a node written earlier. Where a re-extraction has
since renumbered the lots, the honest fix is a full
`python -m pipeline.apply_extractions` rather than this.

Idempotent. Safe to re-run.

Run:  NEO4J_HTTP_API=1 python -m scripts.backfill_field_provenance --dry-run
      NEO4J_HTTP_API=1 python -m scripts.backfill_field_provenance
"""
from __future__ import annotations

import argparse
from collections import Counter

from dotenv import load_dotenv

from api.neo4j_client import run_query, run_read_query
from pipeline.apply_extractions import (
    SCOPE_CONSENSUS, SCOPE_LOT, consensus_and_contested,
    entities_with_corrections, fetch_work, field_provenance, group_lots,
    match_lots_to_listings, sole_claimants,
)
from pipeline.obs import get_logger

log = get_logger(__name__)
load_dotenv()

WRITE_CHUNK = 200

# `a[k] IS NOT NULL` is what keeps this honest: the lists name only properties
# the node actually holds, so a field cleared by some later script is not
# claimed back by a provenance stamp. Both lists are set unconditionally — an
# empty list is the true answer for a listing with nothing left.
_WRITE = """
UNWIND $rows AS row
MATCH (a:AuctionProperty {auction_id: row.aid})
WHERE a.enrichment_source = 'grounded_extraction'
SET a.notice_fields_lot =
      [k IN row.prov_lot WHERE a[k] IS NOT NULL],
    a.notice_fields_consensus =
      [k IN row.prov_consensus WHERE a[k] IS NOT NULL]
RETURN a.auction_id AS aid,
       size(a.notice_fields_lot) AS n_lot,
       size(a.notice_fields_consensus) AS n_consensus
"""


def build_rows(limit: int | None = None) -> list[dict]:
    """Recompute each listing's field provenance from the live extractions.

    Mirrors `apply_extractions.run()`'s field branch exactly and nothing else:
    no description, no agreement verdicts, no lot-match writes.
    """
    rows: list[dict] = []
    work = fetch_work(limit)
    print(f"Documents with grounded extraction: {len(work)}")
    for w in work:
        ents = entities_with_corrections(w["extraction_json"],
                                         w.get("corrections_json"))
        if not ents:
            continue
        lots = group_lots(ents)
        listings = [l for l in (w.get("listings") or []) if l.get("aid")]
        matches, _unmatched = match_lots_to_listings(lots, listings)
        sole = {id(m[0]) for m in sole_claimants(matches)}
        consensus, _contested = consensus_and_contested(lots)
        for listing, lot, _reason in matches:
            is_sole = id(listing) in sole
            safe_fields = lot["fields"] if is_sole else consensus
            if safe_fields:
                rows.append({"aid": listing["aid"],
                             "filename": w["filename"],
                             "props": safe_fields,
                             "scope": SCOPE_LOT if is_sole
                                      else SCOPE_CONSENSUS})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None,
                    help="only the first N documents")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and report, write nothing")
    args = ap.parse_args()

    before = run_read_query("""
        MATCH (a:AuctionProperty)
        WHERE a.enrichment_source = 'grounded_extraction'
        RETURN count(a) AS enriched,
               count(a.notice_fields_lot) AS with_lot,
               count(a.notice_fields_consensus) AS with_consensus
    """)
    if before:
        b = before[0]
        print(f"grounded-enriched listings: {b['enriched']}  "
              f"already stamped: lot={b['with_lot']} "
              f"consensus={b['with_consensus']}")

    rows = build_rows(args.limit)
    prov = field_provenance(rows)
    print(f"listings with computed provenance: {len(prov)}")

    scopes = Counter()
    for aid, p in prov.items():
        scopes[SCOPE_LOT] += len(p[SCOPE_LOT])
        scopes[SCOPE_CONSENSUS] += len(p[SCOPE_CONSENSUS])
        if p[SCOPE_LOT]:
            scopes["listings_with_lot_fields"] += 1
        if p[SCOPE_CONSENSUS]:
            scopes["listings_with_consensus_fields"] += 1
    print(f"  field names from a confirmed lot : {scopes[SCOPE_LOT]:>6} "
          f"across {scopes['listings_with_lot_fields']} listings")
    print(f"  field names from notice consensus: "
          f"{scopes[SCOPE_CONSENSUS]:>6} across "
          f"{scopes['listings_with_consensus_fields']} listings")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return

    payload = [{"aid": aid,
                "prov_lot": p[SCOPE_LOT],
                "prov_consensus": p[SCOPE_CONSENSUS]}
               for aid, p in prov.items()]
    written = 0
    kept_lot = kept_consensus = 0
    for i in range(0, len(payload), WRITE_CHUNK):
        res = run_query(_WRITE, {"rows": payload[i:i + WRITE_CHUNK]})
        for r in (res or []):
            written += 1
            kept_lot += int(r["n_lot"] or 0)
            kept_consensus += int(r["n_consensus"] or 0)
    print(f"\nlistings stamped: {written}")
    print(f"  names kept after the a[k] IS NOT NULL filter: "
          f"lot={kept_lot} consensus={kept_consensus}")
    dropped = (scopes[SCOPE_LOT] + scopes[SCOPE_CONSENSUS]
               - kept_lot - kept_consensus)
    if dropped:
        print(f"  {dropped} computed name(s) dropped — the node does not hold "
              f"that property, so claiming it would have been false")

    after = run_read_query("""
        MATCH (a:AuctionProperty)
        WHERE a.notice_fields_lot IS NOT NULL
           OR a.notice_fields_consensus IS NOT NULL
        RETURN count(a) AS stamped
    """)
    if after:
        print(f"listings now carrying provenance: {after[0]['stamped']}")


if __name__ == "__main__":
    main()
