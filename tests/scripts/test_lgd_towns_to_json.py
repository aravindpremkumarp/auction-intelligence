"""The LGD town adapter: the urban local bodies and the taluks their wards cover.

Pure tests on tiny workbooks shaped like the real exports: a banner, a header
whose "Local Body Name" column repeats (English, then Tamil), an "(In English)"
second header line, sparse ``ss:Index`` cells, and coverage rows that stop at
the district or carry the ward alone.
"""
from __future__ import annotations

from scripts.lgd_towns_to_json import convert


def _cell(i, v):
    return f'<Cell ss:Index="{i}"><Data ss:Type="String">{v}</Data></Cell>'


def _book(rows):
    body = "".join("<Row>" + "".join(_cell(i, v) for i, v in r.items()) + "</Row>"
                   for r in rows)
    return ('<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
            'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet><Table>'
            + body + "</Table></Worksheet></Workbook>")


ULB = [
    {1: "Local Government Body"},
    {1: "Urban Local bodies of Tamil Nadu(State Code:33) State"},
    {1: "S.No.", 2: "Localbody Type Code", 3: "Localbody Type Name",
     4: "Localbody Code", 5: "Localbody Version", 6: "Local Body Name",
     7: "Local Body Name", 8: "Census 2001 Code"},
    {6: "(In English)", 7: "(In Local)"},
    {1: "1.0", 2: "5", 3: "Municipality", 4: "252500", 5: "3", 6: "Attur", 7: "ஆத்தூர்"},
    {1: "2.0", 2: "5", 3: "Municipality", 4: "252700", 5: "1", 6: "Pernambut", 7: "பேர்ணாம்பட்டு"},
    {1: "3.0", 2: "6", 3: "Town Panchayat", 4: "252900", 5: "1", 6: "Kottur", 7: "கோட்டூர்"},
    {1: "Sep 27, 2026, 2:44 PM"},
]

WARDS = [
    {1: "Local Government Directory"},
    {1: "S.No.", 2: "Local Body Code", 3: "Local Body Name", 4: "Ward Code",
     5: "Ward Number", 6: "Ward Name", 7: "District Code", 8: "District Name",
     9: "Subdistrict Code", 10: "Subdistrict Name"},
    # coverage at the ward alone, then to the district, then to the taluk
    {1: "1", 2: "252500", 3: "Attur", 4: "9001", 5: "1", 6: "Attur (M) - Ward No.1"},
    {1: "2", 2: "252500", 3: "Attur", 4: "9001", 5: "1", 6: "Attur (M) - Ward No.1",
     7: "613", 8: "Salem"},
    {1: "3", 2: "252500", 3: "Attur", 4: "9001", 5: "1", 6: "Attur (M) - Ward No.1",
     7: "613", 8: "Salem", 9: "5800", 10: "Attur"},
    {1: "4", 2: "252500", 3: "Attur", 4: "9002", 5: "2", 6: "Attur (M) - Ward No.2",
     7: "613", 8: "Salem", 9: "5800", 10: "Attur"},
    {1: "5", 2: "252700", 3: "Pernambut", 4: "9101", 5: "1", 6: "Pernambut (M) - Ward No.1",
     7: "604", 8: "Vellore", 9: "5700", 10: "Gudiyatham"},
    {1: "6", 2: "252700", 3: "Pernambut", 4: "9102", 5: "2", 6: "Pernambut (M) - Ward No.2",
     7: "604", 8: "Vellore", 9: "5701", 10: "Pernambut"},
]


def _convert(tmp_path):
    ulb, wards = tmp_path / "ulb.xls", tmp_path / "wards.xls"
    ulb.write_text(_book(ULB), encoding="utf-8")
    wards.write_text(_book(WARDS), encoding="utf-8")
    return convert(str(ulb), str(wards))


def test_each_town_gets_its_type_district_and_taluks(tmp_path):
    towns, _ = _convert(tmp_path)
    assert towns["252500"] == {"name": "Attur", "type": "Municipality",
                               "district": "Salem", "taluks": ["Attur"], "wards": 2}
    assert towns["252700"]["taluks"] == ["Gudiyatham", "Pernambut"]


def test_the_english_name_is_kept_not_the_tamil_one(tmp_path):
    towns, _ = _convert(tmp_path)
    assert {t["name"] for t in towns.values()} == {"Attur", "Pernambut", "Kottur"}


def test_a_town_with_no_ward_coverage_is_kept_unplaced(tmp_path):
    towns, stats = _convert(tmp_path)
    assert towns["252900"] == {"name": "Kottur", "type": "Town Panchayat",
                               "district": None, "taluks": [], "wards": 0}
    assert stats["0 taluk(s)"] == 1 and stats["towns"] == 3


def test_the_header_rows_and_footer_are_not_towns(tmp_path):
    towns, _ = _convert(tmp_path)
    assert all(code.isdigit() for code in towns)
    assert list(towns) == sorted(towns, key=int)
