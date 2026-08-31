"""The price-disagreement queue: what it shows and what a verdict settles.

Queue logic is exercised against monkeypatched query functions, the way the
other review tests do — no DB, real payload shapes.
"""
from __future__ import annotations

import pytest

import api.review.queries as q
from pipeline.resolution_review import (
    decided_price_checks, decision_key, price_check_key,
)


# ── the decision key ─────────────────────────────────────────────────────────

def test_key_is_derived_from_the_listing_alone():
    assert decision_key("price-check", {"auction_id": "750895"}) == \
        "price-check:750895"


def test_key_ignores_the_prices_so_a_re_extraction_cannot_reopen_it():
    """A reviewer settles the LISTING.

    If the key carried the two prices, re-extracting the notice and shifting
    the number by a rupee would ask them the same question again.
    """
    assert price_check_key("1") == decision_key(
        "price-check", {"auction_id": "1", "portal_price": 999})


def test_a_payload_with_no_listing_is_refused():
    """decision_key raises KeyError; record_resolution_decision turns it into
    the 422 the endpoint returns. Asserting the raw contract here."""
    with pytest.raises(KeyError):
        decision_key("price-check", {})


# ── what settles a row ───────────────────────────────────────────────────────

def _dec(aid, verdict):
    return {"kind": "price-check", "key": price_check_key(aid),
            "verdict": verdict, "payload": {"auction_id": aid}}


def test_both_verdicts_settle_the_question():
    """The queue asks a human to LOOK.

    'Price is wrong' and 'false alarm' are both answers, so both drop the row
    — what happens to the number afterwards is a separate job.
    """
    assert decided_price_checks(
        [_dec("1", "approved"), _dec("2", "rejected")]) == {"1", "2"}


def test_another_kind_of_decision_does_not_settle_a_price_check():
    assert decided_price_checks(
        [{"kind": "lot-match", "key": "k", "verdict": "approved",
          "payload": {"auction_id": "1"}}]) == set()


def test_a_decision_with_no_listing_is_ignored_rather_than_crashing():
    assert decided_price_checks(
        [{"kind": "price-check", "key": "k", "verdict": "approved",
          "payload": {}}]) == set()


# ── the queue query ──────────────────────────────────────────────────────────

def _queue(monkeypatch, rows, decisions=()):
    captured = {}

    def fake_read(cypher, params=None, **kw):
        captured["cypher"] = cypher
        captured["params"] = params or {}
        return rows

    monkeypatch.setattr(q, "run_read_query", fake_read)
    return q._price_checks(list(decisions)), captured


def test_settled_listings_are_excluded_by_the_query(monkeypatch):
    _, cap = _queue(monkeypatch, [], [_dec("750895", "rejected")])
    assert cap["params"]["settled"] == ["750895"]
    assert "NOT p.auction_id IN $settled" in cap["cypher"]


def test_only_flagged_listings_are_offered(monkeypatch):
    _, cap = _queue(monkeypatch, [])
    assert "p.price_agreement IS NOT NULL" in cap["cypher"]


def test_criticals_come_first(monkeypatch):
    _, cap = _queue(monkeypatch, [])
    assert "price_agreement_severity = 'critical'" in cap["cypher"]


def test_a_single_lot_notice_price_is_reached_through_the_document(monkeypatch):
    """Single-lot notices carry no resolved_lot_key by design.

    Joining only on the key blanks the notice price on exactly the rows that
    need it most — 21 of the first 34 critical findings sat on single-lot
    notices, and a row showing one price is a row a reviewer cannot act on.
    """
    _, cap = _queue(monkeypatch, [])
    assert "lot_count = 1" in cap["cypher"]
    assert "coalesce(ka.reserve_price_num" in cap["cypher"]


def test_rows_pass_through_with_their_evidence(monkeypatch):
    row = {"auction_id": "1", "verdict": "magnitude_slip",
           "severity": "critical", "ratio": 10.0, "portal_price": 4500000.0,
           "notice_price": 450000.0, "lot_count": 1, "filename": "n.jpg"}
    rows, _ = _queue(monkeypatch, [row])
    assert rows == [row]


def test_the_queue_is_bounded(monkeypatch):
    _, cap = _queue(monkeypatch, [])
    assert cap["params"]["limit"] == q._PRICE_CHECK_LIMIT


# ── the endpoint's guard ─────────────────────────────────────────────────────

def test_a_verdict_on_a_listing_that_does_not_exist_is_refused(monkeypatch):
    """The queue only offers real listings, but the endpoint takes any payload.

    A verdict aimed at a missing auction_id would sit in the graph forever,
    silently suppressing nothing.
    """
    monkeypatch.setattr(q, "_count_query", lambda *a, **k: {"n": 0})
    with pytest.raises(ValueError, match="no listing"):
        q.record_resolution_decision(
            "price-check", {"auction_id": "nope"}, "approved", "a@b.c")


def test_a_verdict_on_a_real_listing_is_stored(monkeypatch):
    monkeypatch.setattr(q, "_count_query", lambda *a, **k: {"n": 1})
    monkeypatch.setattr(q, "run_query", lambda *a, **k: [{"n": 1}])
    out = q.record_resolution_decision(
        "price-check", {"auction_id": "750895"}, "approved", "a@b.c")
    assert out["key"] == "price-check:750895"


def test_a_false_alarm_is_checked_too(monkeypatch):
    """Both verdicts write a node, so both need the listing to exist."""
    monkeypatch.setattr(q, "_count_query", lambda *a, **k: {"n": 0})
    with pytest.raises(ValueError, match="no listing"):
        q.record_resolution_decision(
            "price-check", {"auction_id": "nope"}, "rejected", "a@b.c")
