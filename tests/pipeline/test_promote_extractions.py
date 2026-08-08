"""Unit tests for pipeline/promote_extractions.py (pure entity -> graph shaping).

No Neo4j here — build_lots() is the whole decision layer, so testing it covers
the promotion logic without a database.
"""
from __future__ import annotations

import pytest

import pipeline.promote_extractions as P


def ent(cls, text="", **attrs):
    return {"id": "0", "cls": cls, "text": text, "start": 0,
            "end": len(text), "attrs": attrs}


def build(entities, filename="notices/x/n.jpg"):
    return P.build_lots(entities, filename)


# ── identifier value normalization ───────────────────────────────────────────

def test_value_norm_strips_spacing_and_case():
    assert P.value_norm("S.F No 256 / 1F") == P.value_norm("sf no 256/1f")


def test_value_norm_preserves_subdivision():
    # 72/1B and 72/1B1 are different parcels — the suffix must survive
    assert P.value_norm("72/1B") != P.value_norm("72/1B1")


def test_value_norm_empty():
    assert P.value_norm("") is None
    assert P.value_norm(None) is None


# ── lot splitting ────────────────────────────────────────────────────────────

def test_single_lot_defaults_to_index_one():
    _, lots = build([ent("full_description", "All that piece and parcel")])
    assert len(lots) == 1
    assert lots[0]["lot_index"] == "1"
    assert lots[0]["lot_key"] == "notices/x/n.jpg#1"


def test_multi_lot_splits_by_lot_index():
    _, lots = build([
        ent("property", "A", lot_index="1", property_type="land"),
        ent("property", "B", lot_index="2", property_type="flat"),
        ent("identifier", "S.No 1", lot_index="2", kind="survey_old", value="1/2"),
    ])
    assert [l["lot_index"] for l in lots] == ["1", "2"]
    assert len(lots[0]["identifiers"]) == 0
    assert len(lots[1]["identifiers"]) == 1


# ── possession: silence is a fact ────────────────────────────────────────────

def test_possession_recorded_when_stated():
    _, lots = build([ent("property", "x", possession_type="Symbolic")])
    p = lots[0]["props"]
    assert p["possession_type"] == "symbolic"
    assert p["possession_stated"] is True


def test_possession_refusal_is_explicit_not_null():
    # a notice printing the boilerplate disjunction emits no possession_type;
    # that is a refusal to commit, not a failed extraction
    _, lots = build([ent("property", "x", property_type="land")])
    p = lots[0]["props"]
    assert "possession_type" not in p
    assert p["possession_stated"] is False


def test_garbage_possession_value_is_not_stored():
    _, lots = build([ent("property", "x",
                         possession_type="Constructive / Symbolic / Physical")])
    assert "possession_type" not in lots[0]["props"]
    assert lots[0]["props"]["possession_stated"] is False


# ── extents ──────────────────────────────────────────────────────────────────

def test_cent_extent_is_converted():
    _, lots = build([ent("extent", "0.20 cents", total_area="0.20 cents")])
    m = lots[0]["measurements"][0]
    assert m["kind"] == "total"
    assert m["unit"] == "cent"
    assert m["sqft_norm"] == pytest.approx(87.12)
    assert m["norm_method"] == "converted"


def test_stated_sqft_is_not_marked_converted():
    _, lots = build([ent("extent", "2180 sq.ft", total_area="2180 sq.ft")])
    assert lots[0]["measurements"][0]["norm_method"] == "stated"


def test_extent_sqft_attr_is_taken_as_bare_number():
    _, lots = build([ent("extent", "x", extent_sqft="9583.00")])
    m = lots[0]["measurements"][0]
    assert (m["kind"], m["unit"], m["sqft_norm"]) == ("extent", "sq_ft", 9583.0)


def test_uds_parent_is_kept_but_never_headline():
    _, lots = build([ent("extent", "x", undivided_share="509 sq.ft",
                         uds_parent_extent="80854 sq.ft",
                         built_up_area="950 sq.ft")])
    kinds = {m["kind"] for m in lots[0]["measurements"]}
    assert {"uds", "uds_parent", "built_up"} <= kinds
    assert lots[0]["headline_kind"] == "built_up"


# ── boundaries: road width and access kind ───────────────────────────────────

def test_road_width_is_lifted_out_of_adjacency():
    _, lots = build([ent("boundary", "x", side="north",
                         adjacency="23 Feet wide East-West Road",
                         measurement="30 feet")])
    b = lots[0]["boundaries"]["north"]
    assert b["access_kind"] == "road"
    assert b["road_width_ft"] == 23.0
    assert b["measurement_ft"] == 30.0


def test_derived_lot_road_width_is_the_widest_side():
    _, lots = build([
        ent("boundary", "x", side="north", adjacency="20 feet Road"),
        ent("boundary", "x", side="south", adjacency="30 feet Road",
            measurement="45 feet"),
    ])
    p = lots[0]["props"]
    assert p["road_width_ft"] == 30.0
    assert p["frontage_ft"] == 45.0


def test_setback_does_not_count_as_road_frontage():
    # "LAND LEFT BY ROAD" is reserved for widening — it reduces the parcel
    _, lots = build([ent("boundary", "x", side="east",
                         adjacency="30 FT LAND LEFT BY ROAD")])
    assert lots[0]["boundaries"]["east"]["access_kind"] == "setback"
    assert "road_width_ft" not in lots[0]["props"]


def test_area_written_into_a_measurement_is_flagged():
    _, lots = build([ent("boundary", "x", side="west",
                         adjacency="Plot No.6", measurement="19 Sq.Ft")])
    b = lots[0]["boundaries"]["west"]
    assert b["is_length_valid"] is False
    assert b["measurement_ft"] is None


def test_absent_measurement_is_still_valid():
    _, lots = build([ent("boundary", "x", side="north", adjacency="Road")])
    assert lots[0]["boundaries"]["north"]["is_length_valid"] is True


# ── parties ──────────────────────────────────────────────────────────────────

def test_party_role_is_preserved():
    _, lots = build([
        ent("borrower", "Smt. P. Karnagi", role="borrower"),
        ent("borrower", "Sri. Ganeshkumar", role="guarantor"),
    ])
    roles = {p["name"]: p["role"] for p in lots[0]["parties"]}
    assert roles["Sri. Ganeshkumar"] == "guarantor"


def test_unknown_role_falls_back_to_borrower():
    _, lots = build([ent("borrower", "X", role="beneficiary")])
    assert lots[0]["parties"][0]["role"] == "borrower"


# ── notice level ─────────────────────────────────────────────────────────────

def test_notice_level_creditor_fields():
    notice, _ = build([ent("secured_creditor", "Indian Bank",
                           legal_basis="SARFAESI", bank_name="Indian Bank",
                           branch="Portonovo",
                           auction_platform_url="https://baanknet.com")])
    assert notice["legal_basis"] == "SARFAESI"
    assert notice["bank_name"] == "Indian Bank"


def test_terms_block_is_hashed_for_dedup():
    text = "1. EMD shall be forfeited. 2. As is where is."
    n1, _ = build([ent("full_terms", text)])
    n2, _ = build([ent("full_terms", text)], filename="other.jpg")
    assert n1["terms_hash"] == n2["terms_hash"]


def test_notice_level_extras_do_not_land_on_a_lot():
    notice, lots = build([
        ent("extras", "RERA no", key="rera_no", value="TN/1/2024"),
        ent("full_description", "desc"),
    ])
    assert notice["facts"] == [{"key": "rera_no", "value": "TN/1/2024"}]
    assert lots[0]["facts"] == []


def test_lot_level_extras_attach_to_the_lot():
    _, lots = build([ent("extras", "road", key="road_access",
                         value="30ft", lot_index="1")])
    assert lots[0]["facts"] == [{"key": "road_access", "value": "30ft"}]


# ── placeholder scrubbing ────────────────────────────────────────────────────

@pytest.mark.parametrize("junk", ["NULL", "null", "N/A", "-", "  "])
def test_placeholder_values_are_dropped(junk):
    _, lots = build([ent("property", "x", encumbrance=junk,
                         property_type="land")])
    assert "encumbrance" not in lots[0]["props"]


# ── platform naming (cross-source join) ──────────────────────────────────────

@pytest.mark.parametrize("url,name", [
    ("https://baanknet.com", "BAANKNET"),
    ("https://www.mstcecommerce.com/", "MSTCECOMMERCE"),
])
def test_platform_name_from_url(url, name):
    assert P.platform_name_of(url) == name


def test_platform_name_of_none():
    assert P.platform_name_of(None) is None
