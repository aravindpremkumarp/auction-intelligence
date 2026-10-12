"""
scripts/load_habitations.py
---------------------------
Flatten TNRD's habitation (hamlet) list into ``pipeline/lookups/tnrd_habitations.json``.

SOURCE
------
Tamil Nadu Rural Development & Panchayat Raj, "Databases" page
(https://www.tnrd.tn.gov.in/databases.html): ``dist_blk_vill_hab_new*.xlsx``,
one row per habitation —

    lgd dcode | district name | lgd bcode | block name |
    lgd pvcode | Village vname | habitation code | habitation name

``lgd pvcode`` is the gram panchayat's LGD local-body code, the same code
scripts/load_gram_panchayats stores as ``GramPanchayat.code``, so hamlets join
the gazetteer on a code, not a name. ("Village vname" is the panchayat's name,
not a revenue village.)

WHY A FILE, NOT GRAPH NODES
---------------------------
Notices often write a hamlet in the village field ("Puduvayal",
"T.Murungapatti"); place_resolution.panchayat_place reads this list to narrow
such a lot to its panchayat's villages — "one of these", never one answer. That
is a lookup, like lgd_towns.json, and 71k hamlet nodes would each cost a
relationship on a database capped at 400,000 (the first load hit the cap). The
gazetteer joins the file to the graph's panchayat links when it loads.

OUTPUT
------
``{"source": ..., "panchayats": {lgd panchayat code: [habitation name, ...]}}``.
The list is rural: Chennai district is not in it, and only the rural blocks of
the districts around it are.

USAGE
-----
    python -m scripts.load_habitations --from-xlsx dist_blk_vill_hab_new.xlsx
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

from pipeline.place_resolution import HABITATIONS

SOURCE = "TNRD habitations (https://www.tnrd.tn.gov.in/databases.html)"
_COLUMNS = ("lgd pvcode", "habitation code", "habitation name")


def read_rows(path: str) -> list[dict]:
    """Rows of {gp_code, code, name} from the TNRD workbook (any sheet carrying
    the header)."""
    import openpyxl  # dev dependency, config/requirements.txt

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out: list[dict] = []
    for ws in wb.worksheets:
        it = ws.iter_rows(values_only=True)
        header = [str(c or "").strip().lower() for c in next(it, ())]
        if not all(c in header for c in _COLUMNS):
            continue
        at = {c: header.index(c) for c in _COLUMNS}
        for row in it:
            out.append({k: row[i] for k, i in at.items()})
    return [{"gp_code": str(r["lgd pvcode"]).strip(), "code": str(r["habitation code"]).strip(),
             "name": str(r["habitation name"] or "").strip()}
            for r in out if r["lgd pvcode"] is not None]


def build(rows: list[dict]) -> dict:
    """``{panchayat code: sorted unique habitation names}``; blank rows dropped."""
    by_gp: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r["gp_code"] and r["name"]:
            by_gp[r["gp_code"]].add(r["name"])
    return {code: sorted(names) for code, names in sorted(by_gp.items())}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-xlsx", required=True, metavar="PATH")
    ap.add_argument("-o", "--out", default=str(HABITATIONS))
    args = ap.parse_args()
    rows = read_rows(args.from_xlsx)
    panchayats = build(rows)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"source": SOURCE, "panchayats": panchayats}, fh,
                  ensure_ascii=False, indent=0, sort_keys=True)
        fh.write("\n")
    print(f"rows: {len(rows)}  panchayats: {len(panchayats)}  "
          f"habitations: {sum(len(v) for v in panchayats.values())}  -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
