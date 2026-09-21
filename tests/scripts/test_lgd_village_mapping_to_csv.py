"""The LGD adapter: what it pulls out of SpreadsheetML, and what it refuses.

Pure tests — the converter reads a file and writes a file, so a tiny workbook
standing in for the 28 MB export exercises every rule.
"""
from __future__ import annotations

import csv

import pytest

from scripts.lgd_village_mapping_to_csv import convert, iter_rows

#: The shape LGD actually ships: a banner, a blank, the title, another blank, a
#: merged header row, its "(In English)" second line, then data. Column indices
#: are explicit on the header and the first data row, absent on the second —
#: which is how the real file writes them.
WORKBOOK = """<?xml version="1.0"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
          xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
 <Worksheet ss:Name="Report">
  <ss:Table>
   <Row><Cell ss:Index="1"><Data ss:Type="String">Local Government Directory</Data></Cell></Row>
   <Row><Cell ss:Index="1"/></Row>
   <Row><Cell ss:Index="1"><Data ss:Type="String">Village To Gram Panchayat Mapping Of Tamil Nadu(State Code : 33) State</Data></Cell></Row>
   <Row><Cell ss:Index="1"/></Row>
   <Row>
    <Cell ss:Index="1"><Data ss:Type="String">S.No.</Data></Cell>
    <Cell ss:Index="2"><Data ss:Type="String">District Code</Data></Cell>
    <Cell ss:Index="3"><Data ss:Type="String">District Name</Data></Cell>
    <Cell ss:Index="6"><Data ss:Type="String">Subdistrict Code</Data></Cell>
    <Cell ss:Index="7"><Data ss:Type="String">Subdistrict Name</Data></Cell>
    <Cell ss:Index="10"><Data ss:Type="String">Village Code</Data></Cell>
    <Cell ss:Index="11"><Data ss:Type="String">Village Name</Data></Cell>
    <Cell ss:Index="14"><Data ss:Type="String">Local Body Code</Data></Cell>
    <Cell ss:Index="15"><Data ss:Type="String">Local Body Name</Data></Cell>
   </Row>
   <Row>
    <Cell ss:Index="3"><Data ss:Type="String">(In English)</Data></Cell>
    <Cell ss:Index="7"><Data ss:Type="String">(In English)</Data></Cell>
    <Cell ss:Index="11"><Data ss:Type="String">(In English)</Data></Cell>
    <Cell ss:Index="15"><Data ss:Type="String">(In English)</Data></Cell>
   </Row>
   <Row>
    <Cell ss:Index="1"><Data ss:Type="String">1</Data></Cell>
    <Cell ss:Index="3"><Data ss:Type="String">Ariyalur</Data></Cell>
    <Cell ss:Index="7"><Data ss:Type="String">Andimadam</Data></Cell>
    <Cell ss:Index="10"><Data ss:Type="Number">636338</Data></Cell>
    <Cell ss:Index="11"><Data ss:Type="String">Alagapuram</Data></Cell>
    <Cell ss:Index="14"><Data ss:Type="Number">226332</Data></Cell>
    <Cell ss:Index="15"><Data ss:Type="String">Alagapuram</Data></Cell>
   </Row>
   <Row>
    <Cell><Data ss:Type="String">2</Data></Cell>
    <Cell><Data ss:Type="String">610</Data></Cell>
    <Cell><Data ss:Type="String">Ariyalur</Data></Cell>
    <Cell ss:Index="7"><Data ss:Type="String">Andimadam</Data></Cell>
    <Cell ss:Index="10"><Data ss:Type="Number">636343</Data></Cell>
    <Cell><Data ss:Type="String">Andimadam</Data></Cell>
    <Cell ss:Index="14"><Data ss:Type="Number">226333</Data></Cell>
    <Cell><Data ss:Type="String">Andimadam</Data></Cell>
   </Row>
   <Row>
    <Cell ss:Index="3"><Data ss:Type="String">Ariyalur</Data></Cell>
    <Cell ss:Index="10"><Data ss:Type="Number">636400</Data></Cell>
    <Cell ss:Index="11"><Data ss:Type="String">Kattukudipatti</Data></Cell>
   </Row>
  </ss:Table>
 </Worksheet>
</Workbook>
"""


@pytest.fixture
def book(tmp_path):
    p = tmp_path / "mapping.xls"
    p.write_text(WORKBOOK, encoding="utf-8")
    return p


def _rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_the_loaders_four_columns_come_out_of_the_fifteen(book, tmp_path):
    """LGD's Village Code lands in ``lgd_village_code``, never
    ``village_code``: that property is the graph's within-taluk revenue
    serial, on an unrelated scheme, and mixing the two makes the column
    unreadable."""
    out = tmp_path / "v.csv"
    convert(str(book), str(out))
    assert _rows(out)[0] == {
        "district": "Ariyalur", "taluk": "Andimadam", "village": "Alagapuram",
        "lgd_village_code": "636338",
        "gram_panchayat": "Alagapuram", "gram_panchayat_code": "226332"}


def test_a_cell_without_an_index_takes_the_next_column(book, tmp_path):
    """SpreadsheetML omits empty cells and renumbers with ss:Index, so a cell's
    column cannot be read from its position in the row. Row 2 mixes both forms;
    taking order alone would slide "Andimadam" into the village column."""
    out = tmp_path / "v.csv"
    convert(str(book), str(out))
    second = _rows(out)[1]
    assert second["village"] == "Andimadam"
    assert second["taluk"] == "Andimadam"
    assert second["lgd_village_code"] == "636343"


def test_the_banner_and_the_in_english_line_are_not_villages(book, tmp_path):
    """Four banner rows and a second header line sit above the data. The
    "(In English)" line fills district, taluk and village, so the required-column
    test alone would write it as a village of a district called "(In English)"."""
    out = tmp_path / "v.csv"
    result = convert(str(book), str(out))
    assert [r["village"] for r in _rows(out)] == ["Alagapuram", "Andimadam"]
    assert result["no_code"] == 1
    assert "(In English)" not in {r["district"] for r in _rows(out)}


def test_a_row_missing_its_taluk_is_dropped_and_counted(book, tmp_path):
    """The third data row names a district, a village and a village code, but no
    subdistrict. Half a hierarchy cannot be diffed against a hierarchy, so it is
    not guessed at — but it is counted, because a file full of them is a
    finding rather than a quiet 0-row conversion."""
    out = tmp_path / "v.csv"
    result = convert(str(book), str(out))
    assert result["dropped"] == 1
    assert result["written"] == 2
    assert "Kattukudipatti" not in {r["village"] for r in _rows(out)}


def test_a_workbook_that_is_not_this_export_is_refused(tmp_path):
    p = tmp_path / "other.xls"
    p.write_text(
        '<?xml version="1.0"?>\n'
        '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"\n'
        '          xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">\n'
        ' <Worksheet ss:Name="R"><ss:Table>\n'
        '  <Row><Cell><Data ss:Type="String">State Name</Data></Cell></Row>\n'
        '  <Row><Cell><Data ss:Type="String">Tamil Nadu</Data></Cell></Row>\n'
        ' </ss:Table></Worksheet></Workbook>\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        convert(str(p), str(tmp_path / "v.csv"))


def test_rows_stream_one_at_a_time(book):
    """The real export is 28 MB, so the parser must not hold a tree. Each row
    is yielded as a plain dict and cleared, which is what lets the whole file
    convert in about a second."""
    seen = list(iter_rows(str(book)))
    assert len(seen) == 9                    # 4 banner + 2 header + 3 data
    assert seen[0] == {1: "Local Government Directory"}
    assert seen[1] == {}                     # an empty cell yields no entry
