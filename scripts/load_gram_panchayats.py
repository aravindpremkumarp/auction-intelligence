"""
scripts/load_gram_panchayats.py
-------------------------------
Load LGD's village → gram panchayat mapping onto the :RevenueVillage gazetteer.

WHAT IT WRITES
--------------
From the CSV ``scripts/lgd_village_mapping_to_csv.py`` makes of LGD's
"Village To Gram Panchayat Mapping" export, three columns of the source sheet:

    Village Code       (J) -> RevenueVillage.lgd_village_code, only where unset
    Local Body Code    (N) -> GramPanchayat.code
    Local Body Name    (O) -> GramPanchayat.name

as ``(:RevenueVillage)-[:IN_GRAM_PANCHAYAT]->(:GramPanchayat)``. An edge, not
a property, because the mapping is many-to-many: on the 2026-09-21 Tamil Nadu
export 1,498 villages sit under two panchayats (Kuvagam under both Kuvagam and
Vallam), and a panchayat holds up to a dozen villages.

District / Subdistrict / Village Name (C, G, K) only find the village. Rows are
matched with the gazetteer loader's own rules (``resolve_taluk`` and the
resolver's fold), and a row whose name the taluk holds twice is skipped rather
than guessed.

WHAT IT WILL NOT DO
-------------------
Additive only, and it never creates a village: a row naming a village the
graph does not hold is counted and left for refresh_village_gazetteer.
Rows with no local body — the urban villages, 1,818 on that export — carry no
panchayat and only lend their village code.

The resolver uses this to narrow a lot to "one of these" villages, never to
place it (place_resolution.panchayat_place).

USAGE
-----
    python -m scripts.lgd_village_mapping_to_csv INPUT.xls -o tn_villages.csv
    python -m scripts.load_gram_panchayats --from-csv tn_villages.csv          # dry run
    python -m scripts.load_gram_panchayats --from-csv tn_villages.csv --apply
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict

from pipeline.place_resolution import normalize_place
from scripts.refresh_village_gazetteer import (load_graph_taluks,
                                               load_graph_villages, resolve_taluk)
from scripts.score_ink_coverage import nq

BATCH = 500
SOURCE = "LGD village-to-gram-panchayat"


def plan(rows: list[dict], graph: dict, taluks) -> tuple[list[dict], Counter]:
    """One row per (village, panchayat) to write, and what happened to the rest."""
    stats: Counter = Counter()
    out: list[dict] = []
    seen: set[tuple] = set()
    for rec in rows:
        hit = resolve_taluk(taluks, rec)
        if not hit:
            stats["taluk not in graph"] += 1
            continue
        district, taluk = hit
        name = graph.get((district, taluk), {}).get(normalize_place(rec.get("village", "")))
        if not name:
            stats["village not in graph"] += 1
            continue
        gp = (rec.get("gram_panchayat") or "").strip() or None
        gp_code = (rec.get("gram_panchayat_code") or "").strip() or None
        key = (district, taluk, name, gp_code)
        if key in seen:
            stats["duplicate row"] += 1
            continue
        seen.add(key)
        stats["matched, with panchayat" if gp and gp_code else "matched, no panchayat"] += 1
        out.append({"district": district, "taluk": taluk, "village": name,
                    "lgd_village_code": (rec.get("lgd_village_code") or "").strip() or None,
                    "gp": gp if gp_code else None, "gp_code": gp_code if gp else None})
    return out, stats


_WRITE = """
UNWIND $rows AS row
MATCH (v:RevenueVillage {name: row.village})-[:IN_TALUK]->(:Taluk {name: row.taluk})
      -[:IN_DISTRICT]->(:District {name: row.district})
WITH row, collect(v) AS vs
// A name the taluk holds twice is two villages; the row cannot say which.
WHERE size(vs) = 1
WITH row, vs[0] AS v
SET v.lgd_village_code = coalesce(v.lgd_village_code, row.lgd_village_code)
WITH row, v WHERE row.gp_code IS NOT NULL
MERGE (g:GramPanchayat {code: row.gp_code})
ON CREATE SET g.name = row.gp, g.district = row.district, g.taluk = row.taluk,
              g.source = $source, g.loaded_at = datetime()
MERGE (v)-[:IN_GRAM_PANCHAYAT]->(g)
RETURN count(*) AS linked
"""


def run(csv_path: str, apply: bool) -> int:
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    todo, stats = plan(rows, load_graph_villages(), load_graph_taluks())
    per_village = Counter((r["district"], r["taluk"], r["village"]) for r in todo if r["gp_code"])
    panchayats = {r["gp_code"] for r in todo if r["gp_code"]}
    print(f"rows: {len(rows)}")
    for k, n in stats.most_common():
        print(f"  {k}: {n}")
    print(f"villages to link: {len(per_village)} "
          f"(under 2+ panchayats: {sum(1 for n in per_village.values() if n > 1)})")
    print(f"gram panchayats: {len(panchayats)}")
    sizes = defaultdict(set)
    for r in todo:
        if r["gp_code"]:
            sizes[r["gp_code"]].add(r["village"])
    print("villages per panchayat:",
          dict(sorted(Counter(min(len(v), 5) for v in sizes.values()).items())), "(5 = 5+)")
    if not apply:
        print("dry run: nothing written (pass --apply)")
        return 0
    linked = 0
    for i in range(0, len(todo), BATCH):
        res = nq(_WRITE, {"rows": todo[i:i + BATCH], "source": SOURCE})
        linked += res[0][0] if res else 0
    print(f"applied: {linked} village→panchayat links")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-csv", required=True, metavar="PATH",
                    help="output of scripts/lgd_village_mapping_to_csv")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()
    return run(args.from_csv, args.apply)


if __name__ == "__main__":
    sys.exit(main())
