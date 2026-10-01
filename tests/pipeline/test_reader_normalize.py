from __future__ import annotations

import pytest

from pipeline.reader import normalize as N


@pytest.mark.parametrize("quote,unit,expect", [
    ("Rs. 81,00,000/-", None, 8100000),
    ("Rs.70.00 Lakhs", None, 7000000),
    ("57.34", "lakh", 5734000),
    ("Rs.1.25 Cr", None, 12500000),
    ("₹ 9,50,000", "rupees", 950000),
    ("Rs 45 lakh", "crore", 4500000),        # unit word in the quote wins
])
def test_money_ok(quote, unit, expect):
    assert N.money(quote, unit) == (expect, "ok")


@pytest.mark.parametrize("quote,expect", [("RESERVE PRICE 35.15,000/-", 3515000), ("EMD 3.51,500/-", 351500)])
def test_money_repairs_a_dot_in_an_indian_grouping_slot(quote, expect):
    assert N.money(quote) == (expect, "ok") and N.grouping_repaired(quote)
    assert not N.grouping_repaired("Rs.7.00 Lakhs") and N.money("Rs.7.00 Lakhs") == (700000, "ok")
    assert not N.grouping_repaired("Rs.12.50")


@pytest.mark.parametrize("quote", ["Rs.5O,000", "12.50.000", "Rs. 3,1O,000", "35.15.000"])
def test_money_broken_digits_are_illegible_not_a_number(quote):
    assert N.money(quote).state == "illegible"
    assert N.money(quote).value is None


def test_money_none():
    assert N.money("").state == "none"
    assert N.money("Reserve Price").state == "none"


@pytest.mark.parametrize("quote,hint,expect", [
    ("08-05-2026 at 11.00 AM", None, "2026-05-08T11:00"),
    ("15/10/2026", None, "2026-10-15"),
    ("8th May 2026 between 2.00 PM", None, "2026-05-08T14:00"),
    ("May 8, 2026", None, "2026-05-08"),
    ("22-05-2026 before 5:00 pm", "2026-05-22", "2026-05-22T17:00"),
    ("on 30.09.2026", "2026-09-30T10:00", "2026-09-30T10:00"),   # hint digits all present: kept
])
def test_date_ok(quote, hint, expect):
    assert N.date(quote, hint) == (expect, "ok")


def test_date_rejects_a_hint_the_quote_does_not_support():
    # the model "read" 2026-11-15 off a sentence that says 15-10-2026
    assert N.date("15-10-2026", "2026-11-15") == ("2026-10-15", "ok")
    assert N.date("auction on the date below", "2026-11-15").state == "none"


def test_date_illegible():
    assert N.date("32-13-2026").state == "illegible"


def test_area_uses_measures():
    v = N.area("6.00 Cents")
    assert v.state == "ok" and v.value.unit == "cent" and round(v.value.sqft) == 2614
    assert N.area("").state == "none"


def test_possession_commits_only_to_one_kind():
    assert N.possession("the physical possession of which has been taken") == ("physical", "ok")
    assert N.possession("Constructive / Symbolic / Physical Possession").state == "none"
    assert N.possession("").state == "none"
