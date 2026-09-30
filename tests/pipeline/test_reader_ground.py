from __future__ import annotations

from pipeline.reader.ground import Window, contains, locate

MD = ("Lot 1. Reserve Price Rs.50,00,000/- EMD Rs.5,00,000/- situated at "
      "Kelambakkam Village.\n\nLot 2. Reserve Price Rs.75,00,000/- EMD "
      "Rs.7,50,000/- situated at   Padur Village.")
LOT2 = MD.index("Lot 2.")


def test_exact_then_fold_then_fuzzy():
    w = Window(MD, LOT2)
    hit = locate(w, "Rs.75,00,000/-")
    assert hit.anchor == "exact" and MD[hit.start:hit.end] == "Rs.75,00,000/-"
    hit = locate(w, "situated at Padur village")       # case + whitespace differ
    assert hit.anchor == "fold" and MD[hit.start:hit.end] == "situated at   Padur Village"
    hit = locate(w, "EMD Rs.7,50,000 situated at Padur")   # a dropped "/-": fuzzy
    assert hit is not None and hit.anchor == "fuzzy"


def test_never_returns_outside_the_window():
    w = Window(MD, LOT2)
    assert locate(w, "Rs.5,00,000/-") is None            # lot 1's EMD is off-limits
    assert locate(w, "Kelambakkam Village") is None
    w1 = Window(MD, 0, LOT2)
    hit = locate(w1, "Rs.5,00,000/-")
    assert hit and hit.end <= LOT2


def test_short_quotes_never_fuzzy():
    assert locate(Window(MD, LOT2), "No.5") is None
    assert locate(Window(MD, LOT2), "Rs.7,5O,000") is None   # 11 chars, one wrong: dropped


def test_cursor_prefers_the_next_occurrence():
    w = Window(MD)
    first = locate(w, "Reserve Price")
    second = locate(w, "Reserve Price", cursor=first.end)
    assert second.start > first.start


def test_contains_is_folded_membership():
    w = Window(MD, LOT2)
    assert contains(w, "padur")
    assert not contains(w, "Chengalpattu")
    assert not contains(w, "")
