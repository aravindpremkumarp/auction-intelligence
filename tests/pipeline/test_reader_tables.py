from __future__ import annotations

from pathlib import Path

from pipeline.reader.tables import cell_for, column_roles, lot_rows, parse_tables

FIX = Path(__file__).resolve().parents[1].parent / "evals" / "fixtures"

TABLE = ("Notice head.\n\n<table><tr><td>Sl. No</td><td>Description of property</td>"
         "<td>Reserve Price (In Lakhs)</td><td>EMD (Rs.)</td></tr>"
         "<tr><td>1</td><td>Land at Kelambakkam</td><td>57.34</td><td>5,73,400</td></tr>"
         "<tr><td>2</td><td>Flat at Padur</td><td>Rs. 70.00</td><td>7,00,000</td></tr></table>"
         "\n\nTerms follow.")


def test_header_columns_units_and_lot_rows():
    (t,) = parse_tables(TABLE)
    roles = column_roles(t.header)
    assert roles[0][0] == "serial"
    assert roles[1][0] == "description"
    assert roles[2] == ("reserve_price", "lakh")
    assert roles[3] == ("emd", None)
    rows = lot_rows(t)
    assert [r.index for r in rows] == [1, 2]
    c = cell_for(t, rows[1], "reserve_price")
    assert TABLE[c.start:c.end] == "Rs. 70.00"
    e = cell_for(t, rows[0], "emd")
    assert TABLE[e.start:e.end] == "5,73,400"
    assert rows[0].start < rows[0].end <= rows[1].start


def test_row_layout_refused_when_a_row_lacks_money():
    md = TABLE.replace("<td>Rs. 70.00</td>", "<td>see below</td>")
    (t,) = parse_tables(md)
    assert lot_rows(t) is None


def test_no_table_no_rows():
    assert parse_tables("plain prose, no table") == []


def test_colspan_advances_columns():
    md = ("<table><tr><td>A</td><td>Reserve Price</td><td>EMD</td></tr>"
          "<tr><td colspan=\"2\">merged</td><td>Rs.1,00,000</td></tr></table>")
    (t,) = parse_tables(md)
    row = t.rows[0]
    assert row.cell(2) is not None and "1,00,000" in row.cell(2).text
    assert row.cell(1) is None


def test_real_fixture_752245_header_is_recognised():
    md = (FIX / "752245.txt").read_text(encoding="utf-8")
    tables = parse_tables(md)
    assert tables
    t = tables[0]
    roles = {r for r, _ in t.columns.values()}
    assert {"serial", "borrower", "emd", "reserve_price"} <= roles
    rows = lot_rows(t)
    assert rows and len(rows) == 1
    c = cell_for(t, rows[0], "reserve_price")
    assert "2889000" in md[c.start:c.end].replace(",", "")
