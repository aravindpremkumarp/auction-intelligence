"""India Post's directory → PIN → taluk: what an office's name may vote for.

Pure tests: a tiny ``.xlsx`` built in memory stands in for the 11,880-office
export, shaped like the copy converted from India Post's PDF — a blank first
sheet, two empty columns, PINs stored as numbers.
"""
from __future__ import annotations

import zipfile

from scripts.india_post_pin_taluks import (
    learn, office_village, offices, read_xlsx, village_taluks,
)

_SHEET = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
          "<sheetData>{rows}</sheetData></worksheet>")


def _row(n, cells):
    out = []
    for col, value in cells.items():
        if isinstance(value, (int, float)):
            out.append(f'<c r="{col}{n}"><v>{value}</v></c>')
        else:
            out.append(f'<c r="{col}{n}" t="inlineStr"><is><t>{value}</t></is></c>')
    return f'<row r="{n}">{"".join(out)}</row>'


def _xlsx(tmp_path, rows):
    path = tmp_path / "pincode.xlsx"
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = ('xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
           'relationships"')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml",
                   f'<workbook {ns} {rel}><sheets>'
                   '<sheet name="Sheet1" sheetId="1" r:id="rId1"/>'
                   '<sheet name="Table001" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", _SHEET.format(rows=""))
        body = "".join(_row(i + 1, r) for i, r in enumerate(rows))
        z.writestr("xl/worksheets/sheet2.xml", _SHEET.format(rows=body))
    return str(path)


HEADER = {"A": "Column1", "B": "Office Name", "C": "Column3", "D": "Pincode",
          "E": "Delivery/ Non Delivery", "F": "Office Type"}


def test_offices_are_read_from_the_sheet_with_the_header(tmp_path):
    path = _xlsx(tmp_path, [HEADER,
                            {"B": "Alambadi B.O", "D": 605701, "F": "BO"},
                            {"B": "Vriddhachalam H.O", "D": 606001.0, "F": "HO"},
                            {"B": "Page 2 of 372"}])
    assert offices(read_xlsx(path)) == [("Alambadi B.O", "605701"),
                                        ("Vriddhachalam H.O", "606001")]


def test_an_office_name_drops_its_kind_and_any_bracket():
    assert office_village("Athiyur Thirukkai B.O") == "Athiyur Thirukkai"
    assert office_village("Kolathur BO") == "Kolathur"
    assert office_village("Anna Road H.O (Chennai)") == "Anna Road"
    assert office_village("Adyar S.O") == "Adyar"


VILLAGES = [("Alambadi", "Vridhachalam", "Cuddalore"),
            ("Melvalai", "Vridhachalam", "Cuddalore"),
            ("Malavanthangal", "Vridhachalam", "Cuddalore"),
            ("Kolathur", "Mettur", "Salem"),        # a name in two taluks
            ("Kolathur", "Kundrathur", "Kancheepuram"),
            ("Pudur", "Tittakudi", "Cuddalore")]


def test_a_pin_names_the_taluk_most_of_its_offices_are_villages_of():
    rows = [("Alambadi B.O", "605701"), ("Melvalai B.O", "605701"),
            ("Malavanthangal B.O", "605701"), ("Pudur B.O", "605701"),
            ("Somewhere S.O", "605701")]
    # 3 of the 4 voting offices is 75%, under the 80% bar: the PIN names nothing
    assert learn(rows, village_taluks(VILLAGES)) == {}
    rows.remove(("Pudur B.O", "605701"))
    assert learn(rows, village_taluks(VILLAGES))["605701"] == {
        "taluk": "Vridhachalam", "offices": 4, "voting": 3, "share": 1.0}


def test_a_village_name_in_two_taluks_votes_for_neither():
    rows = [("Kolathur B.O", "600099"), ("Kolathur B.O", "600099")]
    assert learn(rows, village_taluks(VILLAGES)) == {}


def test_one_voting_office_is_not_enough():
    assert learn([("Alambadi B.O", "605701")], village_taluks(VILLAGES)) == {}
