"""
scripts/india_post_pin_taluks.py
--------------------------------
Learn which taluk each PIN code lies in from India Post's own post-office
directory, and write ``pipeline/lookups/pin_taluks_indiapost.json``.

WHY THIS EXISTS
---------------
``pipeline/lookups/pin_taluks.json`` is learned from our own placed lots
(scripts/learn_sro_taluks), so it only knows the 61 PINs those lots happen to
write. India Post lists every post office in the Tamil Nadu circle with its
PIN — but no taluk and no district, only postal circle, region and division,
which follow no revenue boundary.

What it does carry is the office's name, and a branch office (B.O) is named
after the village it serves: "Alambadi B.O", "Melvalai B.O". So each office
whose name is exactly one village of the gazetteer votes for that village's
taluk, and a PIN names a taluk when at least :data:`MIN_OFFICES` of its
offices vote and at least :data:`MIN_SHARE` of the votes agree. A name the
gazetteer holds in two taluks votes for neither. Chennai's offices are named
after neighbourhoods ("Adyar S.O"), not revenue villages, so most Chennai PINs
stay unknown here; the learned table still covers the ones our lots write.

INPUT
-----
The India Post directory for the Tamil Nadu circle as an ``.xlsx``: a sheet
whose header names "Office Name" and "Pincode" (the copy converted from India
Post's PDF has two empty columns and a blank first sheet, both skipped). Read
with the standard library — an ``.xlsx`` is a zip of XML — so the repo takes
no spreadsheet dependency for a one-off conversion.

OUTPUT
------
``{pin: {"taluk", "offices", "voting", "share"}}``, the shape of the learned
table plus the office counts, sorted so a re-run diffs cleanly.
``pipeline.place_resolution.load_pin_taluks`` reads both tables; where they
disagree on a PIN, neither is believed.

ONLY A HINT
-----------
A post office's beat crosses taluk lines, so a PIN's taluk is never an answer
by itself: ``taluk_hint_place`` keeps it only when the notice's village is
found inside that taluk. Checked on listings placed to a village, with the
taluk hidden: as a hint verified by the village, 345 right and 0 wrong (18 of
them new with this table); taken as the answer on its own, India Post's
entries were 35 right and 14 wrong, and no office-count or share threshold
made them safe.

USAGE
-----
    NEO4J_HTTP_API=1 python -m scripts.india_post_pin_taluks TN.xlsx          # dry run
    NEO4J_HTTP_API=1 python -m scripts.india_post_pin_taluks TN.xlsx --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path

from pipeline.place_resolution import INDIA_POST_PINS, normalize_place

#: Offices of a PIN that must name a gazetteer village before it names a
#: taluk, and the share of those that must sit in that one taluk.
MIN_OFFICES = 2
MIN_SHARE = 0.8

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
#: "Alambadi B.O", "Adyar S.O", "Vellore H.O", "Kolathur BO" — the office kind.
_OFFICE_KIND = re.compile(r"[\s.]+(?:[BSHP]\.?\s?O|E\.?D\.?\s?[BS]\.?\s?O)\.?\s*$", re.I)
_PAREN = re.compile(r"\([^)]*\)")
_PIN = re.compile(r"^6\d{5}$")


def _col(ref: str) -> int:
    """Spreadsheet column letters of a cell reference → 0-based index."""
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group():
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx(path: str) -> Iterable[list[str]]:
    """Every sheet's rows, as lists of cell text, from an ``.xlsx``."""
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
        rels = {r.get("Id"): r.get("Target") for r in
                ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        for sheet in ET.fromstring(z.read("xl/workbook.xml")).iter(f"{_NS}sheet"):
            target = rels[sheet.get(_REL)].lstrip("/")
            target = target if target.startswith("xl/") else f"xl/{target}"
            for row in ET.fromstring(z.read(target)).iter(f"{_NS}row"):
                cells: dict[int, str] = {}
                for c in row.iter(f"{_NS}c"):
                    v = c.find(f"{_NS}v")
                    if c.get("t") == "inlineStr":
                        text = "".join(t.text or "" for t in c.iter(f"{_NS}t"))
                    elif v is None:
                        continue
                    elif c.get("t") == "s":
                        text = shared[int(v.text)]
                    else:
                        text = v.text or ""
                    cells[_col(c.get("r"))] = text.strip()
                if cells:
                    yield [cells.get(i, "") for i in range(max(cells) + 1)]


def offices(rows: Iterable[list[str]]) -> list[tuple[str, str]]:
    """``(office name, pin)`` for every office row under a header naming
    "Office Name" and "Pincode". A PIN stored as a number reads "605701.0"."""
    out, name_col, pin_col = [], None, None
    for row in rows:
        folded = [c.strip().lower() for c in row]
        if "office name" in folded and "pincode" in folded:
            name_col, pin_col = folded.index("office name"), folded.index("pincode")
            continue
        if name_col is None or len(row) <= max(name_col, pin_col):
            continue
        pin = row[pin_col].split(".")[0].strip()
        if row[name_col] and _PIN.match(pin):
            out.append((row[name_col], pin))
    return out


def office_village(name: str) -> str:
    """"Athiyur Thirukkai B.O" → "Athiyur Thirukkai"; "Anna Road H.O
    (Chennai)" → "Anna Road"."""
    return _OFFICE_KIND.sub("", _PAREN.sub(" ", name).strip()).strip(" .-")


def village_taluks(villages: Iterable[tuple[str, str, str]]) -> dict[str, set[str]]:
    """``{folded village name: {taluk, ...}}`` across the whole gazetteer."""
    index: dict[str, set[str]] = defaultdict(set)
    for name, taluk, _district in villages:
        index[normalize_place(name)].add(taluk)
    return index


def learn(rows: Iterable[tuple[str, str]], index: dict[str, set[str]]) -> dict[str, dict]:
    """The PIN → taluk table: each office naming exactly one gazetteer
    village votes for its taluk."""
    votes: dict[str, Counter] = defaultdict(Counter)
    total: Counter = Counter()
    for name, pin in rows:
        total[pin] += 1
        taluks = index.get(normalize_place(office_village(name)))
        if taluks and len(taluks) == 1:
            votes[pin][next(iter(taluks))] += 1
    table = {}
    for pin, counter in votes.items():
        (taluk, top), voting = counter.most_common(1)[0], sum(counter.values())
        if voting >= MIN_OFFICES and top / voting >= MIN_SHARE:
            table[pin] = {"taluk": taluk, "offices": total[pin], "voting": voting,
                          "share": round(top / voting, 2)}
    return dict(sorted(table.items()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xlsx", help="India Post directory for the Tamil Nadu circle")
    ap.add_argument("--apply", action="store_true", help="write the table")
    ap.add_argument("-o", "--out", default=str(INDIA_POST_PINS), metavar="PATH")
    args = ap.parse_args(argv)

    from scripts.resolve_places import load_gazetteer
    rows = offices(read_xlsx(args.xlsx))
    table = learn(rows, village_taluks(load_gazetteer().villages))
    pins = {pin for _, pin in rows}
    print(f"{len(rows)} office(s), {len(pins)} PIN(s); {len(table)} PIN(s) name a "
          f"taluk (≥{MIN_OFFICES} voting offices, ≥{MIN_SHARE:.0%} agreeing)")
    for pin, row in list(table.items())[:5]:
        print(f"  {pin}  {row}")
    if args.apply:
        Path(args.out).write_text(json.dumps(table, ensure_ascii=False, indent=1) + "\n",
                                  encoding="utf-8")
        print(f"written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
