"""Census 2011 joined to LGD: where each village of a 2011 taluk is today.

Pure tests on rows shaped like the two exports: the Census directory is its own
hierarchy (district row, sub-district row, then villages; towns start with 8),
and LGD gives each village its taluk today beside its 2011 census code.
"""
from __future__ import annotations

from scripts.census2011_taluk_lineage import census_villages, lineage

CENSUS = [
    ["State Code", "District Code", "Sub District Code", "Town-Village Code", "Town-Village Name"],
    ["33", "000", "00000", "000000", "TAMIL NADU"],
    ["33", "612", "00000", "000000", "Dindigul"],
    ["33", "612", "05980", "000000", "Dindigul"],
    ["33", "612", "05980", "636001", "Adalur"],
    ["33", "612", "05980", "636002", "Thottanuthu"],
    ["33", "612", "05980", "803400", "Dindigul (M)"],          # a town
    ["33", "612", "05981", "000000", "Natham"],
    ["33", "612", "05981", "636100", "Kosavapatti"],
    ["32", "590", "05900", "636999", "Elsewhere"],             # another state
]


def test_villages_take_their_2011_district_and_taluk_and_towns_are_skipped():
    out = census_villages(CENSUS)
    assert out == {"636001": ("Dindigul", "Dindigul", "Adalur"),
                   "636002": ("Dindigul", "Dindigul", "Thottanuthu"),
                   "636100": ("Dindigul", "Natham", "Kosavapatti")}


LGD = [
    {"district": "Dindigul", "taluk": "Dindigulwest", "village": "Adalur", "code": "1", "census": "636001"},
    {"district": "Dindigul", "taluk": "Dindiguleast", "village": "Thottanuthu", "code": "2", "census": "636002"},
    {"district": "Dindigul", "taluk": "Natham", "village": "Kosavapatti", "code": "3", "census": "636100"},
    {"district": "Dindigul", "taluk": "Natham", "village": "New Village", "code": "4", "census": "00000"},
]


def test_a_split_taluk_lists_where_its_villages_went():
    table, stats = lineage(census_villages(CENSUS), LGD)
    assert table["taluks"]["Dindigul"] == {"district": "Dindigul",
                                           "now": {"Dindigulwest": 1, "Dindiguleast": 1}}
    assert table["taluks"]["Natham"]["now"] == {"Natham": 1}
    assert stats["2011 taluks now in 2+ taluks"] == 1
    assert stats["no 2011 match"] == 1          # a village created since 2011


def test_only_moved_villages_are_listed_by_folded_name():
    table, _ = lineage(census_villages(CENSUS), LGD)
    assert table["villages"] == {"Dindigul": {"adalur": ["Dindigulwest", "Adalur"],
                                              "totanutu": ["Dindiguleast", "Thottanuthu"]}}
