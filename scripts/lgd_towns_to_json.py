"""
scripts/lgd_towns_to_json.py
----------------------------
Flatten LGD's urban local-body exports into ``pipeline/lookups/lgd_towns.json``,
the town register ``pipeline.place_resolution.town_place`` reads.

WHY THIS EXISTS
---------------
The village register (scripts/lgd_village_mapping_to_csv) is the rural one: it
maps villages to gram panchayats, so a property inside a municipality, a town
panchayat or a corporation finds no village there — and none is owed. Its
notice locates it by town, ward and block ("New T.S.No. 28/13B, Block No. 37,
Ward No.A, Kaivalliar Street, Villupuram"). LGD's urban exports are the
authoritative list of those towns, and the ward-coverage one says which taluk
every ward of every town lies in. That is the bridge: a notice that names a
town but no taluk can still be placed to its taluk.

INPUTS (two files from lgdirectory.gov.in → Download Directory, Tamil Nadu)
-------------------------------------------------------------------------
* ``ulbSpecificState*.xls`` — "Urban Local bodies of <state>": code, type
  (Municipal Corporation / Municipality / Town Panchayat), name.
* ``uLBWardforStateWithCov*.xls`` — "Wards of Urban local bodies along with
  coverage details": every ward of every town with the district and
  sub-district (taluk) it covers. Rows that stop at the district, or carry the
  ward alone, are coverage at a coarser level and add nothing a full row does
  not.

Both are SpreadsheetML 2003 behind an ``.xls`` extension, read with the
village converter's streaming ``iter_rows``.

OUTPUT
------
``{lgd_code: {"name", "type", "district", "taluks": [...], "wards": n}}``,
sorted, so a re-download diffs cleanly. Names are LGD's own, unfolded: the
resolver folds and maps them onto the gazetteer when it reads them, the same
rule as the village converter ("a converter that cleaned names first would hide
what the source actually says"). A town LGD lists with no ward coverage is kept
with empty ``taluks``: it can still label a property as a town, not place it.

USAGE
-----
    python -m scripts.lgd_towns_to_json ULB.xls WARDS_WITH_COV.xls
    python -m scripts.lgd_towns_to_json ULB.xls WARDS_WITH_COV.xls -o other.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from scripts.lgd_village_mapping_to_csv import _fold, iter_rows

OUT = Path(__file__).resolve().parent.parent / "pipeline" / "lookups" / "lgd_towns.json"

#: Header text -> our name, per file. Found by name, not position: LGD has
#: reordered columns before.
ULB_COLUMNS = {"localbody type name": "type", "localbody code": "code",
               "local body name": "name"}
WARD_COLUMNS = {"local body code": "code", "local body name": "name",
                "ward code": "ward", "district name": "district",
                "subdistrict name": "taluk"}


def _header(cells: dict[int, str], wanted: dict[str, str]) -> dict[int, str] | None:
    """``{col: our name}`` when this row is the header naming every column we
    want. A repeated name ("Local Body Name" in English, then in Tamil) keeps
    its first column, the English one."""
    found: dict[int, str] = {}
    for col in sorted(cells):
        name = wanted.get(_fold(cells[col]))
        if name and name not in found.values():
            found[col] = name
    return found if set(found.values()) == set(wanted.values()) else None


def _records(path: str, wanted: dict[str, str]):
    header = None
    for cells in iter_rows(path):
        if header is None:
            header = _header(cells, wanted)
            continue
        rec = {name: cells[col] for col, name in header.items() if col in cells}
        # Every real row carries a numeric local-body code; the "(In English)"
        # sub-header and the footer ("Sep 27, 2026, 2:44 PM") carry none.
        if rec.get("code", "").isdigit():
            yield rec
    if header is None:
        raise SystemExit(f"{path}: no header row naming {sorted(wanted)} — "
                         f"is this the right LGD export?")


def convert(ulb_path: str, wards_path: str) -> tuple[dict, dict]:
    """The town register, and counts of what was read."""
    towns: dict[str, dict] = {}
    for rec in _records(ulb_path, ULB_COLUMNS):
        towns[rec["code"]] = {"name": rec["name"], "type": rec["type"],
                              "district": None, "taluks": [], "wards": 0}

    taluks: dict[str, set[str]] = defaultdict(set)
    districts: dict[str, Counter] = defaultdict(Counter)
    wards: dict[str, set[str]] = defaultdict(set)
    names: dict[str, str] = {}
    for rec in _records(wards_path, WARD_COLUMNS):
        code = rec["code"]
        names.setdefault(code, rec.get("name", ""))
        if rec.get("ward"):
            wards[code].add(rec["ward"])
        if rec.get("district"):
            districts[code][rec["district"]] += 1
        if rec.get("district") and rec.get("taluk"):
            taluks[code].add(rec["taluk"])

    stats = Counter()
    for code in set(towns) | set(wards):
        town = towns.setdefault(code, {"name": names.get(code, ""), "type": None,
                                       "district": None, "taluks": [], "wards": 0})
        if code not in towns or town["type"] is None:
            stats["in wards only"] += 1
        # A town is in one district; should LGD ever cover a ward in a second,
        # the district most of its wards cover is the town's.
        if districts.get(code):
            town["district"] = districts[code].most_common(1)[0][0]
        town["taluks"] = sorted(taluks.get(code, ()))
        town["wards"] = len(wards.get(code, ()))
        stats["towns"] += 1
        stats[f"{len(town['taluks'])} taluk(s)" if len(town["taluks"]) < 2
              else "2+ taluks"] += 1
    stats.update(Counter(t["type"] for t in towns.values() if t["type"]))
    return dict(sorted(towns.items(), key=lambda kv: int(kv[0]))), stats


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ulb", help="LGD 'Urban Local bodies' export (.xls)")
    ap.add_argument("wards", help="LGD 'Wards of Urban local bodies along with "
                                  "coverage details' export (.xls)")
    ap.add_argument("-o", "--out", default=str(OUT), metavar="PATH")
    args = ap.parse_args()

    towns, stats = convert(args.ulb, args.wards)
    Path(args.out).write_text(
        json.dumps(towns, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(towns)} town(s) written to {args.out}")
    for key, n in sorted(stats.items()):
        print(f"  {n:5d}  {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
