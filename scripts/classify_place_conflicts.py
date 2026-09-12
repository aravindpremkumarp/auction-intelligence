"""
scripts/classify_place_conflicts.py
-----------------------------------
Re-read every portal/notice district disagreement and say which kind it is.

``scripts/resolve_places.py`` writes ``place_portal_conflict`` as a boolean, so
the 527 listings it fires on are one undifferentiated pile: the 2019
reorganisation, the Chennai metro's city-vs-district mismatch, and genuine
mis-resolution all look identical. :mod:`pipeline.place_lineage` can tell them
apart; this runs it over the live corpus.

It is deliberately a separate script from ``resolve_places``. That one re-reads
every notice, re-matches the gazetteer and rewrites three edges per property —
far too much to run for a question about a stored comparison. This reads two
strings per listing and writes at most one property.

Usage::

    python -m scripts.classify_place_conflicts            # report only
    python -m scripts.classify_place_conflicts --write    # persist the kind
    python -m scripts.classify_place_conflicts --show 40  # widen the tail

``--write`` sets ``p.place_portal_conflict_kind`` and nothing else. No edge is
touched, no resolved place is changed, and running it twice is a no-op.

Auth: NEO4J_URI/USERNAME/PASSWORD(/DATABASE), same as every other script here.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from pipeline.place_lineage import NOT_COMPARABLE, SAID, classify, needs_review
from pipeline.place_resolution import Gazetteer
from scripts.score_ink_coverage import nq

BATCH = 500


def load_gazetteer() -> Gazetteer:
    """Districts only.

    The full gazetteer loads 17,164 villages to answer a question about 38
    district names; the portal City maps through ``Gazetteer.district``, which
    needs nothing below that level.
    """
    return Gazetteer(districts=[r[0] for r in nq("MATCH (d:District) RETURN d.name")])


def load_pairs() -> list[dict]:
    """Every listing, with the two strings the comparison is made from.

    Listings missing one side are read too rather than filtered out, so the
    report can say how many there are; `classify` names them
    `no-notice-district` / `no-portal-district` and `needs_review` keeps them
    out of the queue.
    """
    rows = nq("""
        MATCH (p:AuctionProperty)
        OPTIONAL MATCH (p)-[:LOCATED_IN_CITY]->(c:City)
        RETURN p.auction_id, c.name, p.revenue_district, p.place_portal_conflict
    """)
    return [{"auction_id": aid, "city": city, "district": district,
             "flagged": bool(flag)}
            for aid, city, district, flag in rows if aid]


def classify_all(rows: list[dict], gaz: Gazetteer) -> list[dict]:
    """Attach ``portal_district`` and ``kind`` to each row.

    The portal city is mapped through the gazetteer rather than compared raw,
    for the same reason ``resolve_places`` does it: "Kanchipuram" and
    "Kancheepuram" are one district spelled two ways, and comparing the strings
    would invent 200 conflicts that do not exist.
    """
    districts = frozenset(gaz.districts)
    out = []
    for row in rows:
        city = row["city"]
        # An unmappable city falls through as its own raw name rather than as
        # None. That is the difference between "this listing has no portal
        # place" and "the gazetteer has no alias for Periyakulam" — and the
        # second is a fixable gap the stored boolean has been swallowing,
        # because `bool(portal and ...)` is false when the mapping returns
        # nothing.
        portal = (gaz.district(city) or city) if city else None
        out.append({**row, "portal_district": portal,
                    "kind": classify(portal, row["district"],
                                     districts=districts)})
    return out


def _line(kind: str, n: int) -> str:
    state = ("review" if needs_review(kind)
             else "  --  " if kind in NOT_COMPARABLE else "  ok  ")
    return f"  {n:>6}  {state}  {kind:<21} {SAID[kind]}"


def report(rows: list[dict], show: int) -> None:
    kinds = Counter(r["kind"] for r in rows)
    flagged = [r for r in rows if r["flagged"]]
    queue = [r for r in rows if needs_review(r["kind"])]

    print(f"{len(rows):>6}  listings")
    print(f"{len(flagged):>6}  currently carry place_portal_conflict = true")
    print()
    for kind, n in kinds.most_common():
        print(_line(kind, n))
    print()
    print(f"  {len(flagged)} flagged  ->  "
          f"{len([r for r in flagged if needs_review(r['kind'])])} still need a "
          f"human, {len([r for r in flagged if not needs_review(r['kind'])])} "
          f"explained")

    # The queue is not a subset of the flagged set. `place_portal_conflict` is
    # false wherever the portal city did not map to a district at all, so the
    # gazetteer gaps it hides have never been on anyone's list.
    missed = [r for r in queue if not r["flagged"]]
    if missed:
        print(f"  {len(missed)} more need a human that the boolean never "
              f"flagged (portal city maps to no district)")

    pairs = Counter((r["portal_district"] or r["city"], r["district"])
                    for r in queue)
    print()
    print(f"  the queue, by district pair (top {show} of {len(pairs)}):")
    for (portal, district), n in pairs.most_common(show):
        # A pair present in both directions cannot be right both ways round —
        # one side is mis-resolving, and that is the strongest lead here.
        both = "  <- both directions" if pairs.get((district, portal)) else ""
        print(f"  {n:>6}  {str(portal):<18} -> {str(district):<18}{both}")


def write_back(rows: list[dict]) -> None:
    payload = [{"auction_id": r["auction_id"], "kind": r["kind"]} for r in rows]
    for i in range(0, len(payload), BATCH):
        nq("""
            UNWIND $rows AS row
            MATCH (p:AuctionProperty {auction_id: row.auction_id})
            SET p.place_portal_conflict_kind = row.kind
        """, {"rows": payload[i:i + BATCH]})
    print(f"\n  wrote place_portal_conflict_kind on {len(payload)} listings")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="persist place_portal_conflict_kind (default: report only)")
    ap.add_argument("--show", type=int, default=25,
                    help="how many district pairs of the remaining queue to list")
    args = ap.parse_args(argv)

    gaz = load_gazetteer()
    rows = classify_all(load_pairs(), gaz)
    report(rows, args.show)
    if args.write:
        write_back(rows)
    else:
        print("\n  (report only — pass --write to store the kind)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
