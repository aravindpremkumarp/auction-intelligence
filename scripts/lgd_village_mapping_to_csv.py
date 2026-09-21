"""
scripts/lgd_village_mapping_to_csv.py
-------------------------------------
Flatten an LGD "Village To Gram Panchayat Mapping" export into the CSV that
``scripts/refresh_village_gazetteer.py`` reads.

WHY THIS EXISTS
---------------
The gazetteer loader takes ``district,taluk,village[,village_code,name_ta]``
and deliberately does not fetch anything itself (see its module docstring).
The one authoritative file that is actually downloadable — the Local Government
Directory's village-to-gram-panchayat mapping for a state, from
``lgdirectory.gov.in`` — arrives as neither CSV nor a real ``.xls``: it is a
SpreadsheetML 2003 XML document with a ``.xls`` extension, ~28 MB for Tamil
Nadu, four banner rows, a two-deep merged header, and every value wrapped in
``<Cell><Data>``. Neither ``csv`` nor ``xlrd`` nor ``openpyxl`` opens it.

So this is the adapter: one pass with the stdlib's streaming XML parser (the
file is too large to hold as a tree twice), out the other side as the loader's
CSV. It converts and reports; it never touches the graph.

WHAT IT KEEPS
-------------
The export's 15 columns carry three hierarchies — LGD codes, Census 2011 codes
and Census 2001 codes — plus the local body (the gram panchayat) each village
maps to. The loader wants four of them:

    District Name      -> district
    Subdistrict Name   -> taluk      (LGD's name for the revenue taluk)
    Village Name       -> village
    Village Code       -> village_code   (the LGD code, not Census)

``Local Body Name`` / ``Local Body Code`` are carried through as
``gram_panchayat`` / ``gram_panchayat_code``. The loader ignores unknown
columns, so the wider CSV feeds it unchanged while keeping the mapping for
whoever wants it next — it is the one fact in this export that no other source
in the repo holds.

WHAT IT WILL NOT DO
-------------------
No name is corrected, folded or renamed here: the loader folds names with the
resolver's own ``normalize_place`` when it diffs, and a converter that
"cleaned" names first would hide what the source actually says. Rows missing
any of district / taluk / village are dropped and counted, because half a
hierarchy cannot be diffed against a hierarchy (same rule as the loader).

CAVEAT ON COVERAGE
------------------
This export maps villages to *gram panchayats*, so it is the rural register.
Villages inside municipalities, town panchayats and corporations are not in it
— which is exactly where the gazetteer's thinnest taluks are (Chennai's 16
taluks, Avadi, Pallavaram). Expect it to fill the rural gaps and leave the
urban ones; an urban local-body export is a separate download.

USAGE
-----
    python -m scripts.lgd_village_mapping_to_csv INPUT.xls -o tn_villages.csv

    # then, dry-run, against the live gazetteer
    python -m scripts.refresh_village_gazetteer --from-csv tn_villages.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
import xml.etree.ElementTree as ET
from collections import Counter

#: SpreadsheetML puts cells, rows and values in this one namespace.
SS = "urn:schemas-microsoft-com:office:spreadsheet"
_ROW = f"{{{SS}}}Row"
_CELL = f"{{{SS}}}Cell"
_DATA = f"{{{SS}}}Data"
_INDEX = f"{{{SS}}}Index"

#: Source header -> our column name. Matched case- and space-insensitively
#: against the header row, so the 15-column layout is found rather than assumed
#: by position: LGD has reordered these before.
COLUMNS = {
    "district name": "district",
    "subdistrict name": "taluk",
    "village name": "village",
    "village code": "village_code",
    "local body name": "gram_panchayat",
    "local body code": "gram_panchayat_code",
}

#: Order written out. district/taluk/village first because that is what the
#: loader requires; the rest are extra it passes over.
FIELDS = ["district", "taluk", "village", "village_code",
          "gram_panchayat", "gram_panchayat_code"]

REQUIRED = {"district", "taluk", "village"}


def _fold(text: str) -> str:
    """Header text, whitespace-collapsed and lowered, for matching only."""
    return " ".join(str(text or "").strip().lower().replace("_", " ").split())


def iter_rows(path: str):
    """Yield each ``<Row>`` as ``{1-based column: text}``, then free it.

    SpreadsheetML omits empty cells and renumbers with ``ss:Index``, so a cell's
    position cannot be taken from its order in the row — a row that skips
    "Subdistrict Census 2001 Code" would shift every column after it. The index
    is tracked explicitly and only advances past cells that carry one.

    ``iterparse`` with ``elem.clear()`` keeps one row in memory at a time; the
    Tamil Nadu export is 28 MB of XML and ~20k rows.
    """
    for _, elem in ET.iterparse(path, events=("end",)):
        if elem.tag != _ROW:
            continue
        cells: dict[int, str] = {}
        col = 0
        for cell in elem:
            if cell.tag != _CELL:
                continue
            index = cell.get(_INDEX)
            col = int(index) if index else col + 1
            data = cell.find(_DATA)
            if data is not None and data.text is not None:
                text = data.text.strip()
                if text:
                    cells[col] = text
        yield cells
        elem.clear()


def convert(path: str, out_path: str) -> dict:
    """Write the loader's CSV; return what was read, kept and dropped."""
    header: dict[int, str] | None = None
    written = 0
    dropped = 0
    no_code = 0
    rows_seen = 0
    districts: Counter[str] = Counter()

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()

        for cells in iter_rows(path):
            rows_seen += 1

            if header is None:
                # The header row is the first one naming the columns we need.
                # Banner rows ("Local Government Directory", the title, blanks)
                # and the "(In English)" sub-header are skipped by the same
                # test rather than by counting rows, which would break the
                # moment LGD adds a line to the banner.
                found = {col: COLUMNS[_fold(text)]
                         for col, text in cells.items() if _fold(text) in COLUMNS}
                if REQUIRED <= set(found.values()):
                    header = found
                continue

            rec = {name: cells[col] for col, name in header.items() if col in cells}
            if not REQUIRED <= rec.keys():
                dropped += 1
                continue
            # The header is two rows deep: the merged name columns carry an
            # "(In English)" annotation underneath them, which satisfies
            # district/taluk/village and would otherwise be written as a
            # village. Every real row carries a numeric LGD village code and
            # that row carries none, so the code is what separates them —
            # rather than skipping a fixed number of rows, which breaks the
            # moment LGD adds a line to the banner.
            if not rec.get("village_code", "").isdigit():
                no_code += 1
                continue
            writer.writerow(rec)
            written += 1
            districts[rec["district"]] += 1

    if header is None:
        raise SystemExit(
            f"{path}: no header row naming District Name / Subdistrict Name / "
            f"Village Name — is this an LGD village-to-gram-panchayat export?")

    return {"rows_seen": rows_seen, "written": written, "dropped": dropped,
            "no_code": no_code, "districts": districts}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="LGD village-to-gram-panchayat export (.xls)")
    ap.add_argument("-o", "--out", required=True, metavar="PATH",
                    help="CSV for scripts.refresh_village_gazetteer")
    args = ap.parse_args()

    result = convert(args.input, args.out)
    districts = result["districts"]

    print(f"{result['rows_seen']} XML row(s) read")
    print(f"{result['written']} village row(s) written to {args.out}")
    if result["dropped"]:
        print(f"{result['dropped']} row(s) dropped for a missing "
              f"district/taluk/village")
    if result["no_code"]:
        print(f"{result['no_code']} row(s) dropped for carrying no numeric "
              f"village code (the header's '(In English)' line)")
    print(f"{len(districts)} district(s)")
    for name, n in districts.most_common(10):
        print(f"  {n:6d}  {name}")
    if len(districts) > 10:
        print(f"  … and {len(districts) - 10} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
