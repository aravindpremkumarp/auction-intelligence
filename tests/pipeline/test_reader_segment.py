"""Segmentation without a reviewer count (pipeline/reader/segment.py)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.langextract_gold import GOLD
from pipeline.reader import segment as SG

FIX = Path(__file__).resolve().parents[2] / "evals" / "fixtures"


# ── the three layouts from tests/pipeline/test_lot_chunks.py ─────────────────
def _price_notice(n: int) -> str:
    lots = [f"No.{i} Borrower B{i}. Property at Sy No {i}/1.\n"
            f"Reserve price: Rs.{i},00,000/- | EMD: Rs.{i}0,000/-\n"
            for i in range(1, n + 1)]
    return ("# SALE NOTICE\nCanara Bank\n\n" + "\n".join(lots)
            + "\nAuction date 24.06.2026. Bids not below the reserve price.\n")


def _serial_notice(order) -> str:
    rows = "".join(
        f'<tr>\n<td rowspan="3">{i}</td>\n<td>MR. B{i}</td>\n'
        f'<td>Rs. {i},00,000/-</td>\n</tr>\n<tr>\n<td>Earnest Money Deposit '
        f'(EMD): - Rs.{i}0,000/-</td>\n</tr>\n<tr>\n<td>Type of possession: - '
        f'Physical</td>\n</tr>\n<tr>\n<td colspan="3">Description: Sy No {i}/1, '
        f'village V{i}.</td>\n</tr>\n'
        for i in order)
    return ("# TATA CAPITAL\n\n<table>\n<tr><th>Sr. No</th><th>Borrower</th>"
            "<th>Reserve Price</th></tr>\n" + rows + "</table>\n\n"
            "The E-auction will take place on 11-08-2026.\n")


def _numbered_notice(n: int, clauses: int = 0) -> str:
    lots = [f"{i}. Name and Details of the Borrower : Mr B{i}\n\n"
            f"Details of the property : Plot {i}, Sy No {i}/2.\n\n"
            f"Amount due Rs.{i},00,000 as on 05.06.2026.\n"
            for i in range(1, n + 1)]
    terms = "".join(f"{i}. The bidder shall comply with clause {i}.\n"
                    for i in range(1, clauses + 1))
    return "# DCB Bank\n\nSale notice.\n\n" + "\n".join(lots) + "\nTerms:\n" + terms


def _row_table(n: int) -> str:
    rows = "".join(f"<tr><td>{i}</td><td>Mr B{i}, Sy No {i}/1, village V{i}</td>"
                   f"<td>{i}0.50</td><td>Rs.{i},00,000</td></tr>" for i in range(1, n + 1))
    return ("# BANK\n\nSale notice.\n\n<table><tr><td>Sl No</td><td>Description</td>"
            "<td>Reserve Price (In Lakhs)</td><td>EMD</td></tr>" + rows +
            "</table>\n\nE-auction on 11-08-2026.\n")


# ── without a count ───────────────────────────────────────────────────────────
def test_price_layout_without_count_when_no_anchors():
    s = SG.segment(_price_notice(4))
    # "No.1 Borrower" lines are numbered anchors, so the price cut is refused;
    # the numbered layout wins.
    assert s.strategy in ("numbered", "price") and len(s.segments) == 4


def test_serial_rows_without_count():
    s = SG.segment(_serial_notice(range(1, 8)))
    assert len(s.segments) == 7
    assert s.strategy in ("table_row", "serial")
    assert [sg.label for sg in s.segments] in (["1", "2", "3", "4", "5", "6", "7"], [None] * 7)


def test_numbered_without_count_ignores_numbered_terms():
    s = SG.segment(_numbered_notice(6, clauses=4))
    assert s.strategy == "numbered" and len(s.segments) == 6


def test_table_rows_are_the_first_choice():
    md = _row_table(5)
    s = SG.segment(md)
    assert s.strategy == "table_row" and len(s.segments) == 5
    assert all(sg.row is not None for sg in s.segments)
    assert all(SG._MONEY.search(md, sg.start, sg.end) for sg in s.segments)
    assert s.head_end <= s.segments[0].start and s.tail_start >= s.segments[-1].end


def test_a_run_not_starting_at_one_is_not_trusted_without_a_count():
    md = _numbered_notice(6).replace("1. Name", "9. Name")   # 9,2,3,4,5,6
    s = SG.segment(md)
    assert s.strategy in ("whole", "numbered")
    if s.strategy == "numbered":
        assert len(s.segments) == 5           # the consecutive run 2..6, all money


def test_with_a_count_the_exact_rule_applies():
    md = _numbered_notice(6)
    assert len(SG.segment(md, 6).segments) == 6
    assert SG.segment(md, 7).strategy in ("whole", "anchors")
    assert SG.segment(md, 1).strategy == "whole"


def test_flattened_table_is_not_cut_by_rows():
    # labels and values on separate lines, the ContextGem 753006 failure shape
    md = ("<table><tr><td>Reserve Price</td></tr><tr><td>Rs.70,00,000</td></tr>"
          "<tr><td>EMD</td></tr><tr><td>Rs.7,00,000</td></tr></table>")
    s = SG.segment(md)
    assert s.strategy != "table_row"


# ── the model fallback ────────────────────────────────────────────────────────
def _boundaries(*pairs):
    return lambda md: SimpleNamespace(lots=[SimpleNamespace(first_words=a, last_words=b, label=None)
                                            for a, b in pairs])


def test_model_anchors_are_verified_and_ordered():
    md = ("Head " * 10 + "S.No S.No S.No " +
          "Lot one begins here with Sy No 1 Rs.1,00,000 and ends here. "
          "Lot two begins here with Sy No 2 Rs.2,00,000 and stops here. Tail.")
    s = SG.segment(md, boundary_reader=_boundaries(
        ("Lot one begins here", "and ends here."), ("Lot two begins here", "and stops here.")))
    assert s.strategy == "anchors" and len(s.segments) == 2
    assert md[s.segments[1].start:].startswith("Lot two")
    # unlocatable / out of order / no money -> whole
    for bad in (_boundaries(("Nowhere words at all", "and ends here."), ("Lot two begins", "stops here.")),
                _boundaries(("Lot two begins here", "and stops here."), ("Lot one begins here", "and ends here.")),
                _boundaries(("Head Head", "Head Head Head"), ("Lot one begins", "and ends here."))):
        assert SG.segment(md, boundary_reader=bad).strategy == "whole"


def test_boundary_reader_not_called_on_a_plain_single_notice():
    called = []
    md = "A short single notice. Reserve price Rs.1,00,000."
    SG.segment(md, boundary_reader=lambda m: called.append(m))
    assert called == []


# ── chunks ────────────────────────────────────────────────────────────────────
def test_chunks_carry_head_table_header_and_tail_and_map_back():
    md = _row_table(7)
    s = SG.segment(md)
    cs = SG.chunks(s, md, lots_per_chunk=5)
    assert [c.segment_ids for c in cs] == [[0, 1, 2, 3, 4], [5, 6]]
    c = cs[1]
    assert "Reserve Price (In Lakhs)" in c.text        # header rows repeated
    assert "E-auction on 11-08-2026" in c.text          # tail repeated
    assert "Mr B6" in c.text and "Mr B2" not in c.text
    i = c.text.index("Mr B6")
    s0, e0 = c.to_notice(i, i + 5)
    assert md[s0:e0] == "Mr B6"
    halves = SG.halve(cs[0], s, md)
    assert [h.segment_ids for h in halves] == [[0, 1], [2, 3, 4]]


def test_whole_is_one_chunk_of_the_notice():
    md = "Plain notice. Reserve price Rs.1,00,000."
    s = SG.segment(md)
    (c,) = SG.chunks(s, md)
    assert c.text == md and c.to_notice(6, 12) == (6, 12)


# ── the gold fixtures: right count or whole, never a wrong cut ────────────────
@pytest.mark.parametrize("g", GOLD, ids=[g["aid"] for g in GOLD])
def test_a_one_lot_notice_is_always_read_whole(g):
    """With a count of 1 the lot IS the notice. A one-row table cut (752245)
    lost the description below the table and every quote in it."""
    md = (FIX / f"{g['aid']}.txt").read_text(encoding="utf-8")
    s = SG.segment(md, 1)
    assert s.whole and s.segments[0].start == 0 and s.segments[0].end == len(md)


@pytest.mark.parametrize("g", GOLD, ids=[g["aid"] for g in GOLD])
def test_fixtures_are_cut_right_or_read_whole(g):
    md = (FIX / f"{g['aid']}.txt").read_text(encoding="utf-8")
    n = len(g["lots"]) if g.get("lots") else 1
    s = SG.segment(md)
    assert s.strategy == "whole" or len(s.segments) == n, (
        f"{g['aid']}: {s.strategy} cut {len(s.segments)} lots, gold has {n}")
    if not s.whole:
        for sg in s.segments:
            assert SG._MONEY.search(md, sg.start, sg.end)


def test_price_cut_keeps_the_terms_printed_after_the_price_with_their_lot():
    """Gold 753006: bid increment, property id and EMD follow each reserve
    price. They belong to that lot, not to the next."""
    import re
    md = (FIX / "753006.txt").read_text(encoding="utf-8")
    s = SG.segment(md, 5)
    assert s.strategy == "price" and len(s.segments) == 5
    ids = [m.start() for m in re.finditer(r"IDIB\d+", md)]
    assert len(ids) == 5
    for i, pos in enumerate(ids):
        seg = s.at(pos)
        assert seg is not None and seg.index == i, (i, pos, seg)
    emds = [m.start() for m in re.finditer(r"EMD\s*:\s*Rs", md)]
    for i, pos in enumerate(emds):
        assert s.at(pos).index == i
