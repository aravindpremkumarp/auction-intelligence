"""Tests for api/agent3/find_by_identifier.py — the standalone lookup tool.

Pure: api.agent3.identifiers.resolve_identifier_detail is monkeypatched.
What's asserted is the grouping and scope-tagging contract, since that's
what an agent actually reads.
"""
from __future__ import annotations

from api.agent3 import find_by_identifier as FBI


def _row(auction_id="A1", kind="survey_new", value="331/1", lot_count=1, **kw):
    base = {"auction_id": auction_id, "matched_kind": kind, "matched_value": value,
            "score": 4.0, "lot_key": "k#1", "lot_property_type": "Land",
            "lot_count": lot_count, "city": "Coimbatore", "bank": "SIB",
            "title": "t", "resolved_lot_key": None}
    base.update(kw)
    return base


def test_no_match_is_a_graph_gap_not_a_property_denial(monkeypatch):
    """A zero result means this graph has no record of the number — it must
    NOT be read as 'this property does not exist'."""
    monkeypatch.setattr(FBI, "resolve_identifier_detail", lambda *a, **k: [])
    out = FBI.find_by_identifier("999/999Z")
    assert out["matches"] == []
    assert "not that the property doesn't exist" in out["hint"]


def test_single_lot_notice_is_scoped_as_lot(monkeypatch):
    monkeypatch.setattr(FBI, "resolve_identifier_detail",
                        lambda *a, **k: [_row(lot_count=1)])
    out = FBI.find_by_identifier("331/1")
    listing = out["matches"][0]["listings"][0]
    assert listing["scope"] == "lot"
    assert "scope_note" not in listing


def test_multi_lot_notice_is_scoped_as_notice(monkeypatch):
    monkeypatch.setattr(FBI, "resolve_identifier_detail",
                        lambda *a, **k: [_row(lot_count=6)])
    out = FBI.find_by_identifier("331/1")
    listing = out["matches"][0]["listings"][0]
    assert listing["scope"] == "notice"
    assert "does not say which one" in listing["scope_note"]


def test_a_resolved_listing_is_lot_scoped_when_it_names_the_matched_lot(monkeypatch):
    """The resolver named THIS lot as the listing's own, and the identifier
    was found on that same lot — safely the property's own."""
    monkeypatch.setattr(FBI, "resolve_identifier_detail", lambda *a, **k: [
        _row(lot_count=6, lot_key="k#3", resolved_lot_key="k#3")])
    out = FBI.find_by_identifier("331/1")
    listing = out["matches"][0]["listings"][0]
    assert listing["scope"] == "lot"
    assert "scope_note" not in listing


def test_a_resolution_to_a_different_lot_does_not_borrow_confidence(monkeypatch):
    """The listing resolved to lot #2, but the identifier was found on lot
    #5 of the same notice — that says NOTHING about whether the identifier
    is this listing's own. Must stay notice-scoped, not silently upgraded."""
    monkeypatch.setattr(FBI, "resolve_identifier_detail", lambda *a, **k: [
        _row(lot_count=6, lot_key="k#5", resolved_lot_key="k#2")])
    out = FBI.find_by_identifier("331/1")
    listing = out["matches"][0]["listings"][0]
    assert listing["scope"] == "notice"


def test_a_parcel_matched_row_with_no_lot_key_is_never_falsely_resolved(monkeypatch):
    """The Parcel-matched branch of `_DETAIL_CYPHER` returns `lot_key=None`
    by construction — a resolved AuctionProperty must not be read as
    confirming a lot the row never named."""
    monkeypatch.setattr(FBI, "resolve_identifier_detail", lambda *a, **k: [
        _row(lot_count=6, lot_key=None, resolved_lot_key="k#2")])
    out = FBI.find_by_identifier("331/1")
    listing = out["matches"][0]["listings"][0]
    assert listing["scope"] == "notice"


def test_listings_sharing_one_notice_are_grouped_under_one_match(monkeypatch):
    """The real shape verified live: 744314 and 744316 are two different
    portal listings that share ONE underlying sale-notice Document. A survey
    number match on that document must read as one finding with two
    listings, not two unrelated-looking hits."""
    monkeypatch.setattr(FBI, "resolve_identifier_detail", lambda *a, **k: [
        _row(auction_id="744314", lot_count=2),
        _row(auction_id="744316", lot_count=2),
    ])
    out = FBI.find_by_identifier("331/1")
    assert len(out["matches"]) == 1
    assert out["matches"][0]["listing_count"] == 2
    ids = {x["auction_id"] for x in out["matches"][0]["listings"]}
    assert ids == {"744314", "744316"}


def test_different_identifier_values_are_separate_matches(monkeypatch):
    monkeypatch.setattr(FBI, "resolve_identifier_detail", lambda *a, **k: [
        _row(auction_id="A1", value="331/1"),
        _row(auction_id="A2", value="331/1, 333/2"),
    ])
    out = FBI.find_by_identifier("331")
    assert out["total_listings"] == 2
    assert len(out["matches"]) == 2


def test_too_short_query_is_an_error(monkeypatch):
    monkeypatch.setattr(FBI, "resolve_identifier_detail", lambda *a, **k: [])
    out = FBI.find_by_identifier("3")
    assert "error" in out


def test_identifier_kind_is_forwarded(monkeypatch):
    """The tool's public parameter is `identifier_kind` — same name as
    find_properties' filter of the same meaning. A mismatched name here is
    exactly the kind of bug an agent (or an eval case) would silently trip
    on: the wrong kwarg raises, `@tool` swallows it as {"error": ...}, and
    the kind filter never actually applied."""
    seen = {}
    def fake(value, kind=None, limit=20):
        seen["kind"] = kind
        return []
    monkeypatch.setattr(FBI, "resolve_identifier_detail", fake)
    FBI.find_by_identifier("331/1", identifier_kind="survey_new")
    assert seen["kind"] == "survey_new"


def test_a_bare_integer_identifier_is_accepted(monkeypatch):
    """Same failure shape as get_property's auction_ids: a plain survey or
    door number ("331") arrives as an int, and a schema-level rejection is
    one the model cannot read."""
    monkeypatch.setattr(FBI, "resolve_identifier_detail",
                        lambda *a, **k: [_row(value="331")])
    out = FBI.find_by_identifier(331)
    assert out["query"] == "331"
    assert out["total_listings"] == 1


def test_the_declared_signature_admits_ints():
    import inspect

    ann = str(inspect.signature(FBI.find_by_identifier).parameters["value"].annotation)
    assert "int" in ann
