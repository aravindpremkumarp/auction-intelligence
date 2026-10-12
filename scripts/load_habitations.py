"""
scripts/load_habitations.py
---------------------------
Load TNRD's habitation (hamlet) list onto the gram panchayats LGD gave us.

SOURCE
------
Tamil Nadu Rural Development & Panchayat Raj, "Databases" page
(https://www.tnrd.tn.gov.in/databases.html): ``dist_blk_vill_hab_new*.xlsx``,
one row per habitation —

    lgd dcode | district name | lgd bcode | block name |
    lgd pvcode | Village vname | habitation code | habitation name

``lgd pvcode`` is the gram panchayat's LGD local-body code, the same code
scripts/load_gram_panchayats stores as ``GramPanchayat.code``, so the join is
on a code, not a name. ("Village vname" is the panchayat's name, not a revenue
village.)

WHAT IT WRITES
--------------
``(:Habitation {gp_code, code, name})-[:IN_GRAM_PANCHAYAT]->(:GramPanchayat)``,
additively. It never creates a panchayat: a row whose code the graph does not
hold is counted and skipped (load the LGD mapping first).

Why: notices often write a hamlet in the village field ("Puduvayal",
"T.Murungapatti"). place_resolution.panchayat_place reads these to narrow such
a lot to its panchayat's villages — "one of these", never a single answer.

The list is rural: Chennai district is not in it, and only the rural blocks of
the districts around it are.

USAGE
-----
    python -m scripts.load_habitations --from-xlsx dist_blk_vill_hab_new.xlsx          # dry run
    python -m scripts.load_habitations --from-xlsx dist_blk_vill_hab_new.xlsx --apply
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from scripts.score_ink_coverage import nq

BATCH = 1000
SOURCE = "TNRD habitations (tnrd.tn.gov.in/databases.html)"
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


def plan(rows: list[dict], known_gp_codes: set[str]) -> tuple[list[dict], Counter]:
    stats: Counter = Counter()
    todo, seen = [], set()
    for r in rows:
        if not r["name"] or not r["gp_code"]:
            stats["blank row"] += 1
        elif r["gp_code"] not in known_gp_codes:
            stats["panchayat not in graph"] += 1
        elif (r["gp_code"], r["code"]) in seen:
            stats["duplicate row"] += 1
        else:
            seen.add((r["gp_code"], r["code"]))
            todo.append(r)
            stats["to load"] += 1
    return todo, stats


_WRITE = """
UNWIND $rows AS row
MATCH (g:GramPanchayat {code: row.gp_code})
MERGE (h:Habitation {gp_code: row.gp_code, code: row.code})
ON CREATE SET h.source = $source, h.loaded_at = datetime()
SET h.name = row.name
MERGE (h)-[:IN_GRAM_PANCHAYAT]->(g)
RETURN count(*) AS linked
"""


def run(path: str, apply: bool) -> int:
    rows = read_rows(path)
    known = {r[0] for r in nq("MATCH (g:GramPanchayat) RETURN g.code")}
    todo, stats = plan(rows, known)
    print(f"rows: {len(rows)}")
    for k, n in stats.most_common():
        print(f"  {k}: {n}")
    print(f"panchayats covered: {len({r['gp_code'] for r in todo})} of {len(known)} in graph")
    if not apply:
        print("dry run: nothing written (pass --apply)")
        return 0
    linked = 0
    for i in range(0, len(todo), BATCH):
        res = nq(_WRITE, {"rows": todo[i:i + BATCH], "source": SOURCE})
        linked += res[0][0] if res else 0
    print(f"applied: {linked} habitations")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-xlsx", required=True, metavar="PATH")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()
    return run(args.from_xlsx, args.apply)


if __name__ == "__main__":
    sys.exit(main())
