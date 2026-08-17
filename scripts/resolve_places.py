"""
scripts/resolve_places.py
-------------------------
Attach every auction property to its official revenue place.

The notice is the trusted source and the portal is only a witness, so this
reads the extracted ``village`` / ``taluk`` / ``district`` already carried on
each :class:`AuctionProperty` (grounded extraction distributed them per lot)
and matches them to the gazetteer with :mod:`pipeline.place_resolution`.

Resolution is per property, not per notice: 531 documents name more than one
village because each lot sits somewhere different, so collapsing a notice to a
single place would be wrong for a third of the corpus.

Writes, all additive — no existing property or relationship is touched::

    p.revenue_district / _taluk / _village   the official names
    p.place_district_source                  'taluk' or 'district'
    p.place_village_status                   resolved / unmatched / absent /
                                             no-parent-taluk /
                                             taluk-has-no-villages
    p.place_notice_conflict                  notice district disagrees with
                                             its own taluk (usually the 2019
                                             reorganisation)
    p.place_portal_conflict                  portal City disagrees with the
                                             resolved district — the tripwire
    p.place_resolved_at

    (p)-[:LOCATED_IN_DISTRICT]->(:District)
    (p)-[:LOCATED_IN_TALUK]->(:Taluk)
    (p)-[:LOCATED_IN_REVENUE_VILLAGE]->(:RevenueVillage)

Usage:
    python -m scripts.resolve_places --dry-run
    python -m scripts.resolve_places

Auth: NEO4J_URI/USERNAME/PASSWORD(/DATABASE).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict

from pipeline.place_resolution import Gazetteer, resolve_place
from scripts.score_ink_coverage import nq

BATCH = 500


def load_gazetteer() -> Gazetteer:
    return Gazetteer(
        districts=[r[0] for r in nq("MATCH (d:District) RETURN d.name")],
        taluks=nq("""MATCH (t:Taluk)-[:IN_DISTRICT]->(d:District)
                     RETURN t.name, d.name"""),
        villages=nq("""MATCH (v:RevenueVillage)-[:IN_TALUK]->(t:Taluk)
                             -[:IN_DISTRICT]->(d:District)
                       RETURN v.name, t.name, d.name"""),
    )


def load_properties() -> list[dict]:
    """Every property with the place strings it will be resolved from.

    ``city`` comes from the portal and is carried only to be disagreed with;
    it never supplies an answer.
    """
    rows = nq("""
        MATCH (p:AuctionProperty)
        OPTIONAL MATCH (p)-[:HAS_DOCUMENT]->(d:Document)
        OPTIONAL MATCH (p)-[:LOCATED_IN_CITY]->(c:City)
        RETURN p.auction_id, p.village, p.taluk, p.district, c.name,
               d.file_path
    """)
    return [{"auction_id": aid, "village": v, "taluk": t, "district": d,
             "city": c, "file_path": fp}
            for aid, v, t, d, c, fp in rows if aid]


def notice_fallback() -> dict[str, dict]:
    """Unambiguous place strings per notice, for properties missing their own.

    Only notices naming exactly one village and one taluk qualify. A multi-lot
    notice spans several places, so borrowing from it would put the property in
    the wrong one — the whole reason resolution runs per property.
    """
    out: dict[str, dict] = {}
    rows = nq("""MATCH (d:Document) WHERE d.extraction_json IS NOT NULL
                 RETURN d.file_path, d.extraction_json""")
    for file_path, ej in rows:
        try:
            entities = json.loads(ej or "[]")
        except (TypeError, ValueError):
            continue
        seen: dict[str, set[str]] = defaultdict(set)
        for e in entities:
            attrs = e.get("attrs") or {}
            for key in ("village", "taluk", "district"):
                value = str(attrs.get(key) or "").strip()
                if value:
                    seen[key].add(value)
        if len(seen["village"]) == 1 and len(seen["taluk"]) <= 1:
            out[file_path] = {
                k: (next(iter(seen[k])) if len(seen.get(k, ())) == 1 else None)
                for k in ("village", "taluk", "district")}
    return out


def write_back(rows: list[dict]) -> None:
    for i in range(0, len(rows), BATCH):
        # Drop this script's own edges first so a re-run replaces rather than
        # accumulates: a property whose village stops resolving must lose its
        # old edge, or the graph keeps an answer the resolver has withdrawn.
        nq("""
            UNWIND $rows AS row
            MATCH (p:AuctionProperty {auction_id: row.auction_id})
                  -[r:LOCATED_IN_DISTRICT|LOCATED_IN_TALUK|LOCATED_IN_REVENUE_VILLAGE]->()
            DELETE r
        """, {"rows": rows[i:i + BATCH]})
        nq("""
            UNWIND $rows AS row
            MATCH (p:AuctionProperty {auction_id: row.auction_id})
            SET p.revenue_district       = row.district,
                p.revenue_taluk          = row.taluk,
                p.revenue_village        = row.village,
                p.place_district_source  = row.district_source,
                p.place_village_status   = row.village_status,
                p.place_village_source   = row.village_source,
                p.place_notice_conflict  = row.notice_conflict,
                p.place_portal_conflict  = row.portal_conflict,
                p.place_resolved_at      = datetime()
        """, {"rows": rows[i:i + BATCH]})
        # Edges are attached with MATCH, never MERGE, on the gazetteer side.
        # Every name written here came out of the gazetteer, so a node that
        # fails to match is a bug worth leaving visible — MERGE would instead
        # invent a duplicate place, and RevenueVillage carries no property
        # naming its taluk, so the duplicate would be unreachable from the
        # hierarchy and silently wrong.
        nq("""
            UNWIND $rows AS row
            WITH row WHERE row.district IS NOT NULL
            MATCH (p:AuctionProperty {auction_id: row.auction_id})
            MATCH (dd:District {name: row.district})
            MERGE (p)-[:LOCATED_IN_DISTRICT]->(dd)
        """, {"rows": rows[i:i + BATCH]})
        nq("""
            UNWIND $rows AS row
            WITH row WHERE row.taluk IS NOT NULL
            MATCH (p:AuctionProperty {auction_id: row.auction_id})
            MATCH (tt:Taluk {name: row.taluk})-[:IN_DISTRICT]->(:District {name: row.district})
            MERGE (p)-[:LOCATED_IN_TALUK]->(tt)
        """, {"rows": rows[i:i + BATCH]})
        nq("""
            UNWIND $rows AS row
            WITH row WHERE row.village IS NOT NULL
            MATCH (p:AuctionProperty {auction_id: row.auction_id})
            MATCH (vv:RevenueVillage {name: row.village})
                  -[:IN_TALUK]->(:Taluk {name: row.taluk})
            MERGE (p)-[:LOCATED_IN_REVENUE_VILLAGE]->(vv)
        """, {"rows": rows[i:i + BATCH]})


def write_state(stats: Counter, total: int, conflicts: list[dict]) -> None:
    nq("""
        MERGE (s:PipelineState {key: 'place_resolution'})
        SET s.updated_at       = datetime(),
            s.properties       = $total,
            s.stats_json       = $stats,
            s.conflicts_json   = $conflicts,
            s.conflicts_open   = $n_conflicts
    """, {"total": total, "stats": json.dumps(dict(stats)),
          "conflicts": json.dumps(conflicts[:400], ensure_ascii=False),
          "n_conflicts": len(conflicts)})


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report only")
    args = ap.parse_args()

    gaz = load_gazetteer()
    props = load_properties()
    fallback = notice_fallback()
    print(f"{len(props)} propert(ies); gazetteer has "
          f"{len(gaz.districts)} districts, {len(gaz.taluks)} taluks, "
          f"{len(gaz.villages)} villages")

    stats: Counter = Counter()
    rows: list[dict] = []
    conflicts: list[dict] = []
    for p in props:
        district, taluk, village = p["district"], p["taluk"], p["village"]
        if not (village or taluk or district):
            fb = fallback.get(p["file_path"] or "")
            if fb:
                district, taluk, village = fb["district"], fb["taluk"], fb["village"]
                stats["filled from notice"] += 1
        res = resolve_place(gaz, district=district, taluk=taluk, village=village)

        # The portal is only ever a witness: its disagreement is recorded, and
        # never allowed to change the answer.
        portal_conflict = False
        if p["city"] and res["district"]:
            portal = gaz.district(p["city"])
            portal_conflict = bool(portal and portal != res["district"])

        if res["village"]:
            stats["village resolved"] += 1
        elif res["taluk"]:
            stats[f"taluk only ({res['village_status']})"] += 1
        elif res["district"]:
            stats["district only"] += 1
        else:
            stats["unresolved"] += 1
        if res["conflict"]:
            stats["notice district vs its taluk"] += 1
            conflicts.append({"auction_id": p["auction_id"],
                              "raw_district": res["raw"]["district"],
                              "taluk": res["taluk"],
                              "resolved_district": res["district"],
                              "kind": "notice"})
        if portal_conflict:
            stats["portal city vs resolved district"] += 1

        rows.append({
            "auction_id": p["auction_id"],
            "district": res["district"], "taluk": res["taluk"],
            "village": res["village"],
            "district_source": res["district_source"],
            "village_status": res["village_status"],
            "village_source": res["village_source"],
            "notice_conflict": res["conflict"],
            "portal_conflict": portal_conflict,
        })

    print()
    for key, n in stats.most_common():
        print(f"  {key:38}{n:>6}  ({100 * n / len(props):.0f}%)")

    if args.dry_run:
        print("\n[dry-run] nothing written")
        return 0

    write_back(rows)
    write_state(stats, len(props), conflicts)
    print(f"\nResolved {len(rows)} propert(ies); {len(conflicts)} district "
          f"conflict(s) stored for review")
    return 0


if __name__ == "__main__":
    sys.exit(main())
