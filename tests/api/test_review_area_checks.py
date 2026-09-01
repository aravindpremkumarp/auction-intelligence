"""The size-contradiction queue: what it shows and what a verdict settles.

Mirrors tests/api/test_review_price_checks.py — same shape of queue, same
kind of verdict — against monkeypatched query functions, no DB.
"""
from __future__ import annotations

import pytest

import api.review.queries as q
from pipeline.resolution_review import (
    area_check_key, decided_area_checks, decision_key,
)


# ── the decision key ─────────────────────────────────────────────────────────

def test_key_is_derived_from_the_listing_alone():
    assert decision_key("area-check", {"auction_id": "812923"}) == \
        "area-check:812923"


def test_key_ignores_the_figures_so_a_re_extraction_cannot_reopen_it():
    """A reviewer settles the LISTING. If the key carried the two areas,
    re-extracting the notice and nudging the measurement would ask them the
    same question again."""
    assert area_check_key("1") == decision_key(
        "area-check", {"auction_id": "1", "listing_sqft": 1997})


def test_a_payload_with_no_listing_is_refused():
    with pytest.raises(KeyError):
        decision_key("area-check", {})


# ── what settles a row ───────────────────────────────────────────────────────

def _dec(aid, verdict):
    return {"kind": "area-check", "key": area_check_key(aid),
            "verdict": verdict, "payload": {"auction_id": aid}}


def test_both_verdicts_settle_the_question():
    """'One of these is wrong' and 'same area after all' are both answers —
    the queue asks a human to look."""
    assert decided_area_checks(
        [_dec("1", "approved"), _dec("2", "rejected")]) == {"1", "2"}


def test_another_kind_of_decision_does_not_settle_an_area_check():
    assert decided_area_checks(
        [{"kind": "price-check", "key": "k", "verdict": "approved",
          "payload": {"auction_id": "1"}}]) == set()


def test_a_decision_with_no_listing_is_ignored_rather_than_crashing():
    assert decided_area_checks(
        [{"kind": "area-check", "key": "k", "verdict": "approved",
          "payload": {}}]) == set()


# ── the queue query ──────────────────────────────────────────────────────────

def _queue(monkeypatch, rows, decisions=()):
    captured = {}

    def fake_read(cypher, params=None, **kw):
        captured["cypher"] = cypher
        captured["params"] = params or {}
        return rows

    monkeypatch.setattr(q, "run_read_query", fake_read)
    return q._area_checks(list(decisions)), captured


def test_settled_listings_are_excluded_by_the_query(monkeypatch):
    _, cap = _queue(monkeypatch, [], [_dec("812923", "rejected")])
    assert cap["params"]["settled"] == ["812923"]
    assert "NOT p.auction_id IN $settled" in cap["cypher"]


def test_only_flagged_listings_are_offered(monkeypatch):
    _, cap = _queue(monkeypatch, [])
    assert "p.area_agreement IS NOT NULL" in cap["cypher"]


def test_criticals_come_first(monkeypatch):
    _, cap = _queue(monkeypatch, [])
    assert "area_agreement_severity = 'critical'" in cap["cypher"]


def test_the_row_carries_both_raw_strings_not_just_the_numbers(monkeypatch):
    """Most of these disagreements are a PARSE difference. A reviewer shown
    only '4625 vs 8611' cannot tell that from a real conflict about land."""
    _, cap = _queue(monkeypatch, [])
    assert "p.total_area AS listing_area" in cap["cypher"]
    assert "collect(m.raw)[0] AS lot_area" in cap["cypher"]


def test_the_lot_side_is_the_headline_measurement(monkeypatch):
    """The figure agent3 serves in the property block — the same one
    area_agreement compared against."""
    _, cap = _queue(monkeypatch, [])
    assert "e.is_headline" in cap["cypher"]


def test_rows_pass_through_with_their_evidence(monkeypatch):
    row = {"auction_id": "812923", "verdict": "disagree", "severity": "med",
           "ratio": 2.24, "listing_area": "1997 Sq. ft.", "listing_sqft": 1997.0,
           "lot_area": "890 sq.ft", "lot_sqft": 890.0,
           "lot_key": "n.jpg#1", "filename": "n.jpg"}
    rows, _ = _queue(monkeypatch, [row])
    assert rows == [row]


def test_the_queue_is_bounded(monkeypatch):
    _, cap = _queue(monkeypatch, [])
    assert cap["params"]["limit"] == q._AREA_CHECK_LIMIT


# ── the endpoint's guard ─────────────────────────────────────────────────────

def test_a_verdict_on_a_listing_that_does_not_exist_is_refused(monkeypatch):
    monkeypatch.setattr(q, "_count_query", lambda *a, **k: {"n": 0})
    with pytest.raises(ValueError, match="no listing"):
        q.record_resolution_decision(
            "area-check", {"auction_id": "nope", "wrong": "listing"},
            "approved", "a@b.c")


def test_a_verdict_must_name_which_figure_is_wrong(monkeypatch):
    """'One of these two numbers is wrong' is not an answer anyone can act
    on, so the vocabulary is checked rather than trusted."""
    monkeypatch.setattr(q, "_count_query", lambda *a, **k: {"n": 1})
    with pytest.raises(ValueError, match="area-check needs wrong"):
        q.record_resolution_decision(
            "area-check", {"auction_id": "1", "wrong": "portal"},
            "approved", "a@b.c")


def test_the_sides_are_named_for_where_the_figure_shows(monkeypatch):
    """Both figures trace to the sale notice — the portal never published an
    area — so 'portal' would be a lie about provenance."""
    assert q.AREA_CHECK_SIDES == {"listing", "lot", "both", "neither"}


def test_a_verdict_on_a_real_listing_is_stored(monkeypatch):
    monkeypatch.setattr(q, "_count_query", lambda *a, **k: {"n": 1})
    monkeypatch.setattr(q, "run_query", lambda *a, **k: [{"n": 1}])
    out = q.record_resolution_decision(
        "area-check", {"auction_id": "812923", "wrong": "lot"},
        "approved", "a@b.c")
    assert out["key"] == "area-check:812923"
