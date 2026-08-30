"""Clear the lot keys that two listings claim, and the ones the retired
auto-resolver wrote.

One lot is one property, so two listings resolving to it cannot both be right
— and both are read as fact: `api/agent3/common.py::scope_of()` treats a
listing with a `resolved_lot_key` as lot-scoped, meaning every value on it is
stated as a fact about THAT property rather than about the notice. A wrong lot
key therefore shows a bidder someone else's property with no hedge on it. No
key at all is the honest state; the listing simply reads as notice-scoped.

Two overlapping sets are cleared:

  * every key written by `system:auto` — `scripts/resolve_lots.py`'s retired
    reserve+borrower pass, which had no rivalry gate. 96 of the 100 keys it
    ever wrote are contested, so the set is treated as untrustworthy whole
    rather than triaged.
  * every contested key regardless of writer. `apply_extractions` gates each
    RUN through `sole_claimants`, but only declines to write — it never clears
    a key an earlier run left behind, so a listing that stops matching keeps
    its stale claim. That is how 20 contested keys carry its name.

Decisions are deleted alongside the keys, since a `ResolutionDecision` that
outlives the value it justified would be re-applied by the next
"Apply my decisions" run. HUMAN decisions are never touched — a person who
opened the notice and picked a lot outranks any rule, this one included.

Afterwards run `python -m pipeline.apply_extractions` to re-derive. Listings
that are still genuinely contested stay NULL, which is correct: the notice
does not separate them, and `sole_claimants` will keep declining.

Usage:
    NEO4J_HTTP_API=1 python -m scripts.clear_rival_lot_matches --dry-run
    NEO4J_HTTP_API=1 python -m scripts.clear_rival_lot_matches

Auth: NEO4J_URI/USERNAME/PASSWORD(/DATABASE).
"""
from __future__ import annotations

import argparse
import collections
import json
import sys

from api.neo4j_client import run_query, run_read_query

#: Written by a rule, so safe to discard and re-derive. Anything else in
#: `decided_by` is a person and is left alone.
AUTOMATED = ("system:auto", "system:apply_extractions")


def survey() -> tuple[dict, dict]:
    """(by auction_id -> {key, writer}, contested lot_key -> [auction_id])."""
    rows = run_read_query(
        """
        MATCH (a:AuctionProperty) WHERE a.resolved_lot_key IS NOT NULL
        OPTIONAL MATCH (r:ResolutionDecision {kind: 'lot-match'})
          WHERE r.key STARTS WITH ('lot-match:' + a.auction_id + '|')
        RETURN a.auction_id AS aid, a.resolved_lot_key AS key,
               r.decided_by AS writer
        """, max_rows=20_000, timeout=120.0)
    listings = {r["aid"]: {"key": r["key"], "writer": r["writer"]} for r in rows}
    by_key: dict[str, list[str]] = collections.defaultdict(list)
    for aid, v in listings.items():
        by_key[v["key"]].append(aid)
    contested = {k: v for k, v in by_key.items() if len(v) > 1}
    return listings, contested


def targets(listings: dict, contested: dict) -> tuple[list[str], list[str]]:
    """(to clear, skipped-because-human)."""
    contested_aids = {aid for aids in contested.values() for aid in aids}
    clear, human = [], []
    for aid, v in listings.items():
        writer = v["writer"]
        is_auto = writer == "system:auto"
        if not (is_auto or aid in contested_aids):
            continue
        # A human's pick outranks every rule. It can still be contested — that
        # is a review problem, not ours to silently undo.
        if writer is not None and writer not in AUTOMATED:
            human.append(aid)
            continue
        clear.append(aid)
    return sorted(clear), sorted(human)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be cleared, write nothing")
    ap.add_argument("--out", metavar="PATH",
                    help="write the cleared (auction_id, lot_key) pairs as JSON "
                         "so the change can be reversed by hand")
    args = ap.parse_args(argv)

    listings, contested = survey()
    clear, human = targets(listings, contested)
    writers = collections.Counter(listings[a]["writer"] for a in clear)

    print(f"listings with a lot key:            {len(listings)}")
    print(f"lots claimed by 2+ listings:        {len(contested)}"
          f"  ({sum(len(v) for v in contested.values())} listings)")
    print(f"\nto clear:                           {len(clear)}")
    for w, n in writers.most_common():
        print(f"    written by {str(w):26} {n}")
    if human:
        print(f"\nleft alone (decided by a person):   {len(human)}")
        for aid in human[:10]:
            print(f"    {aid}  {listings[aid]['key']}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({a: listings[a] for a in clear}, fh, indent=2,
                      ensure_ascii=False)
        print(f"\nreversal record written to {args.out}")

    if args.dry_run:
        print("\n[dry-run] nothing written")
        return 0
    if not clear:
        print("\nnothing to clear")
        return 0
    if not args.out:
        print("\nrefusing to write without --out: clearing is only reversible "
              "if the old values are saved first")
        return 1

    removed = 0
    for i in range(0, len(clear), 200):
        batch = clear[i:i + 200]
        run_query(
            """
            UNWIND $aids AS aid
            MATCH (a:AuctionProperty {auction_id: aid})
            REMOVE a.resolved_lot_key, a.lot_resolved_at
            """, {"aids": batch})
        # The decision justified a value that no longer exists; leaving it
        # would let the next "Apply my decisions" run put the key straight back.
        run_query(
            """
            UNWIND $aids AS aid
            MATCH (r:ResolutionDecision {kind: 'lot-match'})
            WHERE r.key STARTS WITH ('lot-match:' + aid + '|')
              AND (r.decided_by IS NULL OR r.decided_by IN $automated)
            DETACH DELETE r
            """, {"aids": batch, "automated": list(AUTOMATED)})
        removed += len(batch)
    print(f"\ncleared {removed} listing(s)")
    print("next: python -m pipeline.apply_extractions   (re-derives from the "
          "live extraction; genuinely contested listings stay unset)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
