"""Tests for api/agent3/reauction_history.py.

The point of this tool is keeping two signals apart: what the notice STATES
(attempt_no) and what the pipeline INFERRED (SAME_PROPERTY_AS). Conflating
them turns an inference into a stated fact.
"""
from __future__ import annotations

from api.agent3 import reauction_history as RH


def _stub(monkeypatch, subject=None, linked=None):
    def fake(cypher, params=None, timeout=10.0, max_rows=200):
        if "SAME_PROPERTY_AS" in cypher:
            return list(linked or [])
        return [subject] if subject else []
    monkeypatch.setattr(RH, "run_read_query", fake)


def _subject(price=4100000.0, lot_count=1, attempts=None):
    return {"auction_id": "802076", "reserve_price": price,
            "auction_start": None, "lot_count": lot_count,
            "attempts": attempts if attempts is not None else [
                {"attempt_no": 1, "reserve_price": price, "emd": None,
                 "auction_start": None, "sarfaesi_stage": None,
                 "outcome": None}]}


def _link(aid="755956", price=4558000.0, confidence="high"):
    return {"auction_id": aid, "reserve_price": price, "auction_start": None,
            "bank": "Bank of Baroda", "confidence": confidence,
            "match_reason": "borrower_location_desc"}


def test_first_time_listing_is_stated_plainly_not_as_a_gap(monkeypatch):
    """Most properties are first-time listings. That is the common case, not
    missing data."""
    _stub(monkeypatch, subject=_subject(), linked=[])
    out = RH.reauction_history("748779")
    assert out["found"] is True
    assert "No earlier attempt recorded" in out["summary"]


def test_attempt_no_alone_reports_failure_without_inventing_a_drop(monkeypatch):
    """attempt_no says it failed before; it carries NO earlier price. The
    summary must not imply one."""
    _stub(monkeypatch, subject=_subject(attempts=[
        {"attempt_no": 2, "reserve_price": 5130000, "emd": None,
         "auction_start": None, "sarfaesi_stage": None, "outcome": None}]),
        linked=[])
    out = RH.reauction_history("792946")
    assert out["highest_attempt_no"] == 2
    assert "attempt 2" in out["summary"]
    assert "not recorded here" in out["summary"]


def test_a_linked_listing_yields_a_real_price_drop(monkeypatch):
    """₹45.58L → ₹41L is a live example. This is where the drop comes from —
    never from attempt_no."""
    _stub(monkeypatch, subject=_subject(price=4100000.0), linked=[_link()])
    out = RH.reauction_history("802076")
    change = out["earlier_listings"][0]["price_change"]
    assert change["from"] == 4558000.0
    assert change["to"] == 4100000.0
    assert change["percent"] == -10.0


def test_the_ten_percent_convention_is_named_not_sold_as_a_bargain(monkeypatch):
    """A ~10% cut is the standard SARFAESI reduction applied by rule, not a
    discount specific to this property."""
    _stub(monkeypatch, subject=_subject(price=4100000.0, attempts=[
        {"attempt_no": 2, "reserve_price": 4100000.0, "emd": None,
         "auction_start": None, "sarfaesi_stage": None, "outcome": None}]),
        linked=[_link()])
    out = RH.reauction_history("802076")
    assert "standard SARFAESI reduction" in out["summary"]


def test_link_without_a_stated_attempt_is_flagged_as_inferred(monkeypatch):
    """A SAME_PROPERTY_AS match is a pipeline inference. When the notice
    itself does NOT mark a re-auction, saying so is the honest framing."""
    _stub(monkeypatch, subject=_subject(price=4100000.0), linked=[_link()])
    out = RH.reauction_history("802076")
    assert "inferred, not stated" in out["summary"]


def test_confidence_is_surfaced_on_every_link(monkeypatch):
    """high vs medium is the difference between a strong and a weak claim."""
    _stub(monkeypatch, subject=_subject(), linked=[_link(confidence="medium")])
    out = RH.reauction_history("802076")
    assert out["earlier_listings"][0]["confidence"] == "medium"


def test_identical_prices_produce_no_price_change(monkeypatch):
    """A re-listing at the same price is not a drop."""
    _stub(monkeypatch, subject=_subject(price=4100000.0),
          linked=[_link(price=4100000.0)])
    out = RH.reauction_history("802076")
    assert "price_change" not in out["earlier_listings"][0]


def test_multi_lot_notice_scopes_the_attempt(monkeypatch):
    """attempt_no comes from the notice, which may cover several lots."""
    _stub(monkeypatch, subject=_subject(lot_count=3, attempts=[
        {"attempt_no": 2, "reserve_price": 1.0, "emd": None,
         "auction_start": None, "sarfaesi_stage": None, "outcome": None}]),
        linked=[])
    out = RH.reauction_history("X")
    assert out["scope"] == "notice"
    assert "3 lots" in out["scope_note"]


def test_the_dedupe_keeps_one_row_per_linked_listing():
    """A pair can carry SAME_PROPERTY_AS in both directions; the undirected
    match then returns the same listing twice. Caught live on 802076."""
    assert "collect(r) AS rels" in RH._LINKED
    assert "WITH o, collect" in RH._LINKED


def test_unknown_id_is_reported(monkeypatch):
    _stub(monkeypatch, subject=None)
    out = RH.reauction_history("NOPE")
    assert out["found"] is False
