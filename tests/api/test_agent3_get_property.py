"""Tests for api/agent3/get_property.py — the agent3 diligence tool.

The two things worth pinning here are the ones a prompt cannot enforce:
the return SHAPE makes a scope error unwriteable on a multi-lot notice, and
`gaps` names what the notice omits.
"""
from __future__ import annotations

from api.agent3 import get_property as GP


def _stub(monkeypatch, *, listings, docs=None, lots=None):
    def fake(cypher, params=None, timeout=10.0, max_rows=200):
        if "AS notice_url" in cypher:
            return list(docs or [])
        if "AS lot_key" in cypher:
            return list(lots or [])
        return list(listings)
    monkeypatch.setattr(GP, "run_read_query", fake)


def _listing(auction_id="A1", **kw):
    base = {"auction_id": auction_id, "title": "t", "url": "u",
            "description": "d", "description_len": 1, "reserve_price": 4000000.0,
            "reserve_price_raw": "40,00,000", "emd": 400000.0, "emd_raw": "4,00,000",
            "auction_start": None, "auction_end": None, "application_deadline": None,
            "contact_details": "9000000000", "service_provider": "BAANKNET",
            "total_area_raw": None, "undivided_share": None, "door_new": None,
            "door_old": None, "sro": None, "bank": "SBI", "branch": "Anna Nagar",
            "city": "Chennai", "area": "Anna Nagar", "district": "Chennai",
            "taluk": None, "revenue_village": None, "asset_category": "Residential",
            "auction_type": "SARFAESI Auction", "property_types": ["Flat"],
            "borrowers": ["B"], "same_property_as": []}
    base.update(kw)
    return base


def _doc(auction_id="A1", **kw):
    base = {"auction_id": auction_id, "notice_url": "http://n.pdf",
            "filename": "n.pdf", "notice_type": "sale", "doc_type": "notice",
            "sale_terms": "terms", "parse_quality": 0.9, "platform": "BAANKNET",
            "legal_framework": "SARFAESI", "issuing_bank": "SBI",
            "emd_accounts": [{"account_name": "x", "account_no": "1",
                              "ifsc": "SBIN0001", "mode_of_payment": "NEFT"}],
            "contacts": [{"phone": "900", "email": "a@b.c"}],
            "officers": [{"name": "O", "role": "Authorised Officer"}],
            "case_references": [], "trusts": []}
    base.update(kw)
    return base


def _lot(auction_id="A1", lot_key="k#1", sqft=714.0, headline=True,
         identifier_kinds=("survey_old", "patta"), possession="physical",
         encumbrance="nil known", boundaries=True, **kw):
    base = {
        "auction_id": auction_id, "lot_key": lot_key, "lot_index": "1",
        "address": "addr", "village": "V", "taluk": "T", "district": "D",
        "asset_category": "Residential", "property_type": "land and building",
        "encumbrance": encumbrance, "road_width_ft": 20.0, "frontage_ft": None,
        "construction_type": None, "occupancy_status": None, "landmark": None,
        "full_description": "desc", "possession_type": possession,
        "possession_taken_on": None,
        "extents": ([{"kind": "total", "is_headline": headline, "sqft": sqft,
                      "raw": f"{sqft} sq ft", "unit": "sq_ft"}] if sqft else []),
        "identifiers": [{"kind": k, "value": "1/1"} for k in identifier_kinds],
        "boundaries": ([{"side": "north", "adjacent": "road", "length_ft": 40.0,
                         "road_width_ft": 20.0, "access_kind": "road"}]
                       if boundaries else []),
        "loans": [], "parties": [{"name": "B", "role": "borrower"}],
        "title_holders": [], "schedules": [],
        "auctions": [{"attempt_no": 1, "reserve_price": 4000000,
                      "emd": 400000, "bid_increment": 25000,
                      "auction_start": "2026-09-01T11:00", "auction_end": None,
                      "inspection": "2026-08-25T11:00",
                      "application_deadline": None, "sarfaesi_stage": None,
                      "auto_extension_minutes": 5.0}],
    }
    base.update(kw)
    return base


# ── scope ────────────────────────────────────────────────────────────────

def test_single_lot_notice_is_the_property(monkeypatch):
    _stub(monkeypatch, listings=[_listing()], docs=[_doc()], lots=[_lot()])
    out = GP.get_property("A1", depth="full")
    prop = out["properties"][0]
    assert prop["scope"] == "lot"
    assert prop["notice_lot_count"] == 1
    assert prop["property"]["headline_sqft"] == 714.0
    assert "scope_note" not in prop
    assert "notice_lots" not in prop


def test_multi_lot_notice_has_no_flat_property_field_at_all(monkeypatch):
    """The shape is the enforcement. There is no `property` key to read a
    single size out of, so 'this property is 7,040 sqft' cannot be written
    from this payload — only 'the notice covers 2 lots, 3,359-7,040 sqft'."""
    _stub(monkeypatch, listings=[_listing("A2")], docs=[_doc("A2")],
          lots=[_lot("A2", "k#1", sqft=7040.0), _lot("A2", "k#2", sqft=3359.0)])
    out = GP.get_property("A2", depth="full")
    prop = out["properties"][0]
    assert prop["scope"] == "notice"
    assert "property" not in prop
    assert prop["notice_summary"]["lot_count"] == 2
    assert prop["notice_summary"]["sqft_range"] == [3359.0, 7040.0]
    assert len(prop["notice_lots"]) == 2
    assert "2 lots" in prop["scope_note"]


def test_a_resolved_lot_on_a_multi_lot_notice_reads_as_the_property_s_own(monkeypatch):
    """The whole point of the resolver: once `AuctionProperty.resolved_lot_key`
    is set, a multi-lot notice's listing gets the SAME shape a single-lot
    notice gets — `property`, not `notice_lots`, and no scope_note."""
    # Resolved lot is "k#2", listed SECOND — proves the property comes from
    # matching `resolved_lot_key`, not from taking whichever lot sorts first.
    _stub(monkeypatch, listings=[_listing("A2", resolved_lot_key="k#2")],
          docs=[_doc("A2")],
          lots=[_lot("A2", "k#1", sqft=7040.0), _lot("A2", "k#2", sqft=3359.0)])
    out = GP.get_property("A2", depth="full")
    prop = out["properties"][0]
    assert prop["scope"] == "lot"
    assert prop["notice_lot_count"] == 2
    assert prop["property"]["lot_key"] == "k#2"
    assert prop["property"]["headline_sqft"] == 3359.0
    assert "scope_note" not in prop
    assert "notice_lots" not in prop
    assert "resolved_lot_key" not in prop["listing"], \
        "internal bookkeeping field leaked into the model-facing payload"


def test_standard_depth_withholds_the_lot_list_but_keeps_the_summary(monkeypatch):
    _stub(monkeypatch, listings=[_listing("A2")], docs=[_doc("A2")],
          lots=[_lot("A2", "k#1"), _lot("A2", "k#2")])
    out = GP.get_property("A2")
    prop = out["properties"][0]
    assert "notice_lots" not in prop
    assert "depth='full'" in prop["notice_lots_hint"]
    assert prop["notice_summary"]["lot_count"] == 2


def test_a_listing_with_no_readable_lot_says_so(monkeypatch):
    _stub(monkeypatch, listings=[_listing()], docs=[_doc()], lots=[])
    out = GP.get_property("A1")
    prop = out["properties"][0]
    assert prop["scope"] == "notice"
    assert any("No sale-notice lot" in g for g in prop["gaps"])


# ── gaps ─────────────────────────────────────────────────────────────────

def test_missing_patta_is_reported(monkeypatch):
    _stub(monkeypatch, listings=[_listing()], docs=[_doc()],
          lots=[_lot(identifier_kinds=("survey_old",))])
    gaps = GP.get_property("A1")["properties"][0]["gaps"]
    assert any("patta" in g.lower() for g in gaps)


def test_missing_survey_number_is_reported(monkeypatch):
    _stub(monkeypatch, listings=[_listing()], docs=[_doc()],
          lots=[_lot(identifier_kinds=("door_old",))])
    gaps = GP.get_property("A1")["properties"][0]["gaps"]
    assert any("survey number" in g.lower() for g in gaps)


def test_unstated_possession_is_not_reported_as_no_encumbrance(monkeypatch):
    """The dangerous reading. A notice that says nothing about encumbrance is
    not a notice that says there is none."""
    _stub(monkeypatch, listings=[_listing()], docs=[_doc()],
          lots=[_lot(possession=None, encumbrance=None)])
    gaps = GP.get_property("A1")["properties"][0]["gaps"]
    assert any("Possession is not stated" in g for g in gaps)
    assert any("unstated, not as 'no encumbrance'" in g for g in gaps)


def test_a_complete_notice_reports_no_gaps(monkeypatch):
    _stub(monkeypatch, listings=[_listing()], docs=[_doc()], lots=[_lot()])
    assert GP.get_property("A1")["properties"][0]["gaps"] == []


def test_missing_emd_account_and_contact_are_reported(monkeypatch):
    _stub(monkeypatch, listings=[_listing(contact_details=None)],
          docs=[_doc(emd_accounts=[], contacts=[])], lots=[_lot()])
    gaps = GP.get_property("A1")["properties"][0]["gaps"]
    assert any("EMD account" in g for g in gaps)
    assert any("contact" in g.lower() for g in gaps)


# ── extents ──────────────────────────────────────────────────────────────

def test_uds_is_never_used_as_the_property_area(monkeypatch):
    """A flat owns an undivided share of a larger plot. Reading the parent
    extent as the flat's own area turns 760 sqft into 2,257."""
    lot = _lot(sqft=None)
    lot["extents"] = [
        {"kind": "uds", "is_headline": False, "sqft": 365.0, "raw": "", "unit": "sq_ft"},
        {"kind": "uds_parent", "is_headline": False, "sqft": 2257.0, "raw": "", "unit": "sq_ft"},
    ]
    _stub(monkeypatch, listings=[_listing()], docs=[_doc()], lots=[lot])
    prop = GP.get_property("A1", depth="full")["properties"][0]
    assert prop["property"].get("headline_sqft") is None
    assert any("No usable extent" in g for g in prop["gaps"])


def test_out_of_band_extent_is_dropped_and_flagged(monkeypatch):
    lot = _lot(sqft=15_571_959_480.0)
    _stub(monkeypatch, listings=[_listing()], docs=[_doc()], lots=[lot])
    prop = GP.get_property("A1", depth="full")["properties"][0]
    assert prop["property"].get("headline_sqft") is None
    assert "parse errors" in prop["property"]["extent_warning"]


def test_non_headline_total_extent_is_an_acceptable_fallback(monkeypatch):
    _stub(monkeypatch, listings=[_listing()], docs=[_doc()],
          lots=[_lot(sqft=714.0, headline=False)])
    prop = GP.get_property("A1", depth="full")["properties"][0]
    assert prop["property"]["headline_sqft"] == 714.0


# ── input handling ───────────────────────────────────────────────────────

def test_too_many_ids_is_an_error_with_the_cap_named(monkeypatch):
    _stub(monkeypatch, listings=[])
    out = GP.get_property([f"A{i}" for i in range(9)])
    assert "error" in out and "5" in out["error"]


def test_unknown_id_is_reported_not_invented(monkeypatch):
    _stub(monkeypatch, listings=[])
    out = GP.get_property("NOPE")
    assert out["properties"] == []
    assert out["not_found"] == ["NOPE"]
    assert "do not guess" in out["hint"]


def test_partial_miss_lists_only_the_missing_id(monkeypatch):
    _stub(monkeypatch, listings=[_listing("A1")], docs=[_doc()], lots=[_lot()])
    out = GP.get_property(["A1", "GONE"])
    assert out["not_found"] == ["GONE"]
    assert len(out["properties"]) == 1


def test_bad_depth_returns_the_valid_values(monkeypatch):
    _stub(monkeypatch, listings=[])
    out = GP.get_property("A1", depth="everything")
    assert out["valid_values"] == ["standard", "full"]


def test_empty_ids_is_an_error(monkeypatch):
    _stub(monkeypatch, listings=[])
    assert "error" in GP.get_property([])


# ── model-shaped input ───────────────────────────────────────────────────

def test_an_integer_auction_id_is_accepted(monkeypatch):
    """auction_ids look like numbers, so a model sends them as ints. Observed
    in the first real-model smoke run: the model passed `auction_ids: 744314`
    and burned THREE of six model calls retrying it verbatim, because
    pydantic rejects at the schema boundary — before our error-as-data
    decorator can hand back anything the model could learn from."""
    _stub(monkeypatch, listings=[_listing("744314")], docs=[_doc("744314")],
          lots=[_lot("744314")])
    out = GP.get_property(744314)
    assert out["properties"][0]["auction_id"] == "744314"


def test_a_list_of_integer_auction_ids_is_accepted(monkeypatch):
    _stub(monkeypatch, listings=[_listing("744314")], docs=[_doc("744314")],
          lots=[_lot("744314")])
    out = GP.get_property([744314])
    assert out["properties"][0]["auction_id"] == "744314"


def test_the_declared_signature_admits_ints():
    """The runtime coercion is not enough on its own — pydantic builds the
    tool schema from the annotation, so a str-only hint rejects the call
    before any of our code runs."""
    import inspect

    ann = str(inspect.signature(GP.get_property).parameters["auction_ids"].annotation)
    assert "int" in ann


# ── the portal city never sits beside the notice's district ──────────────────

def test_the_portal_city_is_dropped_when_the_notice_resolved_a_district(monkeypatch):
    """Returning both put the same listing in two places at once. The notice
    is the legal document and the portal only a witness, so where resolution
    reached the listing the witness is not quoted alongside it."""
    _stub(monkeypatch,
          listings=[_listing(city="Chennai", district="Chengalpattu")],
          docs=[_doc()], lots=[_lot()])

    listing = GP.get_property("A1")["properties"][0]["listing"]

    assert listing["district"] == "Chengalpattu"
    assert "city" not in listing


def test_the_portal_city_survives_where_no_notice_district_exists(monkeypatch):
    """The portal is the fallback, never the override: a listing place
    resolution never reached must not lose the only location it has."""
    _stub(monkeypatch,
          listings=[_listing(city="Chennai", district=None)],
          docs=[_doc()], lots=[_lot()])

    listing = GP.get_property("A1")["properties"][0]["listing"]

    assert listing["city"] == "Chennai"
    assert "district" not in listing
