"""
scripts/census2011_taluk_lineage.py
-----------------------------------
Which taluk of today each village of a 2011 taluk now lies in, from two
official registers, written to ``pipeline/lookups/taluk_lineage.json``.

WHY THIS EXISTS
---------------
Notices still name taluks as they were before Tamil Nadu split them: "Dindigul"
for what is now Dindigul East and West, "Tiruppur" for Tiruppur North and South,
"Chengalpattu" for land now in Tiruporur or Vandalur. The resolver either
refuses the old name (``no-parent-taluk``) or, when the old name is still one
of the new taluks, looks for the village there and misses (``unmatched``).

No register states the splits, and the taluks created since 2011 carry an
empty census code in LGD, so their parent cannot be read off the taluk list.
But every village keeps its 2011 Census code:

* the Census 2011 town and village directory (``PC11_TV_DIR``) puts each
  village code under its 2011 sub-district (taluk);
* LGD's village list for the state carries the same code beside the village's
  taluk today.

Joined on the code, each village says where it was and where it is. That is
the lineage — official, village by village, nothing assumed from names.

INPUTS
------
* ``PC11_TV_DIR.xlsx`` — Census 2011 town and village directory, all India
  (state 33 is read). Rows are the hierarchy itself: a district row (sub-
  district ``00000``, code ``000000``), a sub-district row (code ``000000``),
  then its villages; town codes start with 8 and are skipped — Chennai's old
  taluks were all towns, so they get no lineage here.
* LGD's "villages of a specific state" export (``villageofSpecificState*.xls``,
  SpreadsheetML), read with the village converter's ``iter_rows``.

OUTPUT
------
``{"taluks":   {2011 taluk: {"district": 2011 district, "now": {taluk: villages}}},
   "villages": {2011 taluk: {folded village: [taluk now, village now]}}}``

``villages`` holds only villages whose taluk changed name, keyed by both the
2011 and today's spelling, folded with the resolver's ``normalize_place``.
Names are LGD's and the Census's own ("Dindiguleast"); the resolver maps them
onto the gazetteer when it reads them.

USAGE
-----
    python -m scripts.census2011_taluk_lineage PC11_TV_DIR.xlsx LGD_VILLAGES.xls
    python -m scripts.census2011_taluk_lineage PC11_TV_DIR.xlsx LGD_VILLAGES.xls -o other.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path

from pipeline.place_resolution import TALUK_LINEAGE, normalize_place
from scripts.india_post_pin_taluks import read_xlsx
from scripts.lgd_towns_to_json import _header
from scripts.lgd_village_mapping_to_csv import iter_rows

TAMIL_NADU = "33"
LGD_COLUMNS = {"district name": "district", "sub-district name": "taluk",
               "village code": "code", "village name": "village",
               "census 2011 code": "census"}


def census_villages(rows: Iterable[list[str]], state: str = TAMIL_NADU
                    ) -> dict[str, tuple[str, str, str]]:
    """``{2011 village code: (district, taluk, village)}`` for one state."""
    districts: dict[str, str] = {}
    taluks: dict[str, str] = {}
    out = {}
    for row in rows:
        if len(row) < 5 or row[0] != state:
            continue
        _, district, taluk, code, name = row[:5]
        if taluk == "00000" and code == "000000":
            districts[district] = name
        elif code == "000000":
            taluks[taluk] = name
        elif not code.startswith("8"):        # 8xxxxx: towns, not villages
            out[code.lstrip("0")] = (districts.get(district), taluks.get(taluk), name)
    return out


def lgd_villages(path: str) -> Iterable[dict]:
    """LGD's village rows, by column name."""
    header = None
    for cells in iter_rows(path):
        if header is None:
            header = _header(cells, LGD_COLUMNS)
            continue
        rec = {name: cells[col] for col, name in header.items() if col in cells}
        if rec.get("code", "").isdigit():
            yield rec


def lineage(census: dict[str, tuple[str, str, str]], lgd: Iterable[dict]) -> tuple[dict, Counter]:
    """The lineage table, and counts of what joined."""
    taluks: dict[str, dict] = {}
    now: dict[str, Counter] = defaultdict(Counter)
    villages: dict[str, dict[str, list[str]]] = defaultdict(dict)
    stats = Counter()
    for rec in lgd:
        old = census.get((rec.get("census") or "").lstrip("0"))
        if not old or not old[1]:
            stats["no 2011 match"] += 1
            continue
        stats["joined"] += 1
        district_2011, taluk_2011, village_2011 = old
        taluks.setdefault(taluk_2011, {"district": district_2011})
        now[taluk_2011][rec["taluk"]] += 1
        if normalize_place(rec["taluk"]) != normalize_place(taluk_2011):
            stats["moved to a differently named taluk"] += 1
            entry = [rec["taluk"], rec["village"]]
            for name in (village_2011, rec["village"]):
                villages[taluk_2011].setdefault(normalize_place(name), entry)
    for name, counter in now.items():
        taluks[name]["now"] = dict(counter.most_common())
    stats["2011 taluks"] = len(taluks)
    stats["2011 taluks now in 2+ taluks"] = sum(len(c) > 1 for c in now.values())
    return ({"taluks": dict(sorted(taluks.items())),
             "villages": {k: dict(sorted(v.items())) for k, v in sorted(villages.items())}},
            stats)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("census", help="Census 2011 town and village directory (PC11_TV_DIR.xlsx)")
    ap.add_argument("lgd", help="LGD villages of a specific state export (.xls)")
    ap.add_argument("-o", "--out", default=str(TALUK_LINEAGE), metavar="PATH")
    args = ap.parse_args(argv)

    table, stats = lineage(census_villages(read_xlsx(args.census)), lgd_villages(args.lgd))
    Path(args.out).write_text(json.dumps(table, ensure_ascii=False, indent=1) + "\n",
                              encoding="utf-8")
    print(f"written to {args.out}")
    for key, n in sorted(stats.items()):
        print(f"  {n:6d}  {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
