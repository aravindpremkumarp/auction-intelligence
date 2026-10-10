"""pipeline/lot_pairing: each lot's description sits with its own reserve
price and borrower. Pure — builds extraction stand-ins over a real string."""
from __future__ import annotations

from types import SimpleNamespace

from pipeline.lot_pairing import lot_pairing
from pipeline.validators import validate


def E(cls, md, text, lot, **attrs):
    """Extraction grounded at the first occurrence of ``text`` in ``md``."""
    s = md.index(text)
    a = {"lot_index": lot, **attrs}
    return SimpleNamespace(extraction_class=cls, extraction_text=text, attributes=a,
                           char_interval=SimpleNamespace(start_pos=s, end_pos=s + len(text)))


def _ents(md, pairs):
    """pairs: [(lot, description, reserve_text, reserve_num)]."""
    out = []
    for lot, desc, rtext, rnum in pairs:
        out.append(E("full_description", md, desc, lot))
        out.append(E("auction_terms", md, rtext, lot, reserve_price_num=rnum))
    return out


DESC_ABOVE = ("Lot A: land in Survey No 12, Karur village.\nReserve Price Rs.10,00,000\n\n"
              "Lot B: house in Door No 5, Salem town.\nReserve Price Rs.20,00,000\n")
DESC_BELOW = ("Reserve Price Rs.10,00,000\nLot A: land in Survey No 12, Karur village.\n\n"
              "Reserve Price Rs.20,00,000\nLot B: house in Door No 5, Salem town.\n")


def test_description_above_price_is_clean():
    ents = _ents(DESC_ABOVE, [("1", "land in Survey No 12", "Rs.10,00,000", 1_000_000),
                              ("2", "house in Door No 5", "Rs.20,00,000", 2_000_000)])
    r = lot_pairing(ents, DESC_ABOVE)
    assert r["crossed"] == [] and r["split"] == {}


def test_description_below_price_is_clean():
    ents = _ents(DESC_BELOW, [("1", "land in Survey No 12", "Rs.10,00,000", 1_000_000),
                              ("2", "house in Door No 5", "Rs.20,00,000", 2_000_000)])
    assert lot_pairing(ents, DESC_BELOW)["crossed"] == []


def test_swapped_pairs_are_flagged():
    ents = _ents(DESC_ABOVE, [("1", "land in Survey No 12", "Rs.20,00,000", 2_000_000),
                              ("2", "house in Door No 5", "Rs.10,00,000", 1_000_000)])
    assert lot_pairing(ents, DESC_ABOVE)["crossed"] == [("1", "2")]
    report = validate(ents, DESC_ABOVE)
    assert "lot_pairing_off" in {i["code"] for i in report["issues"]}


def test_price_table_then_descriptions_is_clean():
    md = ("Lot 1 Reserve Rs.10,00,000. Lot 2 Reserve Rs.20,00,000.\n\n"
          "Schedule: land in Survey No 12. Schedule: house in Door No 5.")
    ents = _ents(md, [("1", "land in Survey No 12", "Rs.10,00,000", 1_000_000),
                      ("2", "house in Door No 5", "Rs.20,00,000", 2_000_000)])
    assert lot_pairing(ents, md)["crossed"] == []


def test_price_written_twice_is_not_judged():
    # Both lots cost the same: grounding puts both quotes at the first copy,
    # so the position says nothing about which lot the price belongs to.
    md = ("land in Survey No 12. Reserve Rs.10,00,000\n"
          "house in Door No 5. Reserve Rs.10,00,000\n")
    ents = _ents(md, [("1", "house in Door No 5", "Rs.10,00,000", 1_000_000),
                      ("2", "land in Survey No 12", "Rs.10,00,000", 1_000_000)])
    assert lot_pairing(ents, md)["crossed"] == []


def test_single_lot_is_never_flagged():
    ents = _ents(DESC_ABOVE, [("1", "land in Survey No 12", "Rs.20,00,000", 2_000_000)])
    assert lot_pairing(ents, DESC_ABOVE) == {"crossed": [], "split": {}, "layout": ""}


TABLE = ("<table><tr><th>Sr. No</th><th>Borrower</th><th>Description of property</th>"
         "<th>Reserve Price</th></tr>"
         "<tr><td>1</td><td>Mr Arun</td><td>Land in Survey No 12, Karur village</td>"
         "<td>Rs.10,00,000</td></tr>"
         "<tr><td>2</td><td>Mrs Banu</td><td>House in Door No 5, Salem town</td>"
         "<td>Rs.20,00,000</td></tr></table>")


def test_table_row_borrower_in_other_row_is_split():
    ents = _ents(TABLE, [("1", "Land in Survey No 12", "Rs.10,00,000", 1_000_000),
                         ("2", "House in Door No 5", "Rs.20,00,000", 2_000_000)])
    ents += [E("borrower", TABLE, "Mrs Banu", "1"), E("borrower", TABLE, "Mr Arun", "2")]
    r = lot_pairing(ents, TABLE)
    assert r["layout"] == "table_row"
    assert r["split"] == {"1": ["borrower"], "2": ["borrower"]}


def test_table_rows_read_correctly_are_clean():
    ents = _ents(TABLE, [("1", "Land in Survey No 12", "Rs.10,00,000", 1_000_000),
                         ("2", "House in Door No 5", "Rs.20,00,000", 2_000_000)])
    ents += [E("borrower", TABLE, "Mr Arun", "1"), E("borrower", TABLE, "Mrs Banu", "2")]
    r = lot_pairing(ents, TABLE)
    assert r["split"] == {} and r["crossed"] == []


def test_misplaced_span_is_not_judged():
    # The quote is lot 2's own text, but its span landed on lot 1's words
    # (a fuzzy alignment). Position says nothing, so nothing is flagged.
    ents = _ents(DESC_ABOVE, [("1", "land in Survey No 12", "Rs.20,00,000", 2_000_000),
                              ("2", "house in Door No 5", "Rs.10,00,000", 1_000_000)])
    ents[0].char_interval.start_pos = DESC_ABOVE.index("house in Door No 5")
    assert lot_pairing(ents, DESC_ABOVE)["crossed"] == []


def test_quote_matches_through_html_tags():
    md = ("<table><tr><td>1</td><td>Land in <b>Survey No 12</b></td><td>Rs.10,00,000</td></tr>"
          "<tr><td>2</td><td>House in <b>Door No 5</b></td><td>Rs.20,00,000</td></tr></table>")
    s1, s2 = md.index("Land in"), md.index("House in")
    ents = [
        SimpleNamespace(extraction_class="full_description", extraction_text="Land in Survey No 12",
                        attributes={"lot_index": "1"},
                        char_interval=SimpleNamespace(start_pos=s1, end_pos=s1 + 30)),
        SimpleNamespace(extraction_class="full_description", extraction_text="House in Door No 5",
                        attributes={"lot_index": "2"},
                        char_interval=SimpleNamespace(start_pos=s2, end_pos=s2 + 28)),
        E("auction_terms", md, "Rs.20,00,000", "1", reserve_price_num=2_000_000),
        E("auction_terms", md, "Rs.10,00,000", "2", reserve_price_num=1_000_000),
    ]
    assert lot_pairing(ents, md)["crossed"] == [("1", "2")]


def test_shared_borrower_is_not_split():
    # One borrower owns both lots and is named once, in lot 1's row.
    ents = _ents(TABLE, [("1", "Land in Survey No 12", "Rs.10,00,000", 1_000_000),
                         ("2", "House in Door No 5", "Rs.20,00,000", 2_000_000)])
    ents += [E("borrower", TABLE, "Mr Arun", "1"), E("borrower", TABLE, "Mr Arun", "2")]
    assert lot_pairing(ents, TABLE)["split"] == {}
