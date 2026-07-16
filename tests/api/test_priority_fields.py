"""Priority-field weighting in the validator (pipeline/validators.validate).

The reviewer-weighted fields — full_description (critical), then property_type,
possession, extent, UDS, borrower and reserve price (high) — must dominate the
quality score. Pure test: builds extraction objects directly, no langextract.
"""
from __future__ import annotations

from types import SimpleNamespace

from pipeline.validators import validate


def E(cls, text="x", lot="1", grounded=True, **attrs):
    a = dict(attrs)
    if lot is not None:
        a["lot_index"] = lot
    ci = SimpleNamespace(start_pos=0, end_pos=max(len(text), 1)) if grounded else None
    return SimpleNamespace(extraction_class=cls, extraction_text=text,
                           attributes=a, char_interval=ci)


def _codes(ex):
    return {i["code"] for i in validate(ex)["issues"]}


def _sev(ex, code):
    return next(i["severity"] for i in validate(ex)["issues"] if i["code"] == code)


# ── property_type ─────────────────────────────────────────────────────────────
def test_missing_property_type_is_flagged_high():
    ex = [E("property", asset_category="immovable")]   # no property_type
    assert "missing_property_type" in _codes(ex)
    assert _sev(ex, "missing_property_type") == "high"


def test_property_type_present_not_flagged():
    assert "missing_property_type" not in _codes([E("property", property_type="flat")])


# ── possession (Option A: penalise invalid only, blank is fine) ───────────────
def test_possession_invalid_flagged_but_valid_and_blank_are_not():
    assert "possession_type_invalid" in _codes(
        [E("property", property_type="flat", possession_type="leased")])
    assert "possession_type_invalid" not in _codes(
        [E("property", property_type="flat", possession_type="Physical")])
    assert "possession_type_invalid" not in _codes(
        [E("property", property_type="flat")])          # blank is legit


# ── UDS (flats only) ──────────────────────────────────────────────────────────
def test_missing_uds_fires_for_flat_without_share():
    ex = [E("property", property_type="flat"), E("extent", total_area="100 sqft")]
    assert "missing_uds" in _codes(ex)


def test_missing_uds_not_for_land_or_when_uds_present():
    land = [E("property", property_type="vacant land"), E("extent", total_area="100")]
    assert "missing_uds" not in _codes(land)
    flat_ok = [E("property", property_type="flat"),
               E("extent", undivided_share="300 sqft")]
    assert "missing_uds" not in _codes(flat_ok)


# ── extent weight ─────────────────────────────────────────────────────────────
def test_missing_extent_is_high_not_low():
    assert _sev([E("property", property_type="flat")], "missing_extent") == "high"


# ── full_description is the most-weighted field ───────────────────────────────
def test_missing_full_description_is_critical():
    ex = [E("property", property_type="flat"), E("location", village="X")]
    assert _sev(ex, "missing_full_description") == "critical"


def test_full_description_absence_costs_thirty_points():
    complete = [
        E("secured_creditor", legal_basis="SARFAESI", bank_name="X"),
        E("borrower", role="borrower"),
        E("property", property_type="flat"),
        E("location", village="X"),
        E("extent", undivided_share="300 sqft"),
        E("auction_terms", reserve_price_num="1000000"),
        E("full_description", text="all that flat and parcel"),
    ]
    with_fd = validate(complete)["score"]
    without_fd = validate([e for e in complete
                           if e.extraction_class != "full_description"])["score"]
    assert with_fd == 100
    assert with_fd - without_fd == 30      # critical penalty


# ── lot anchors: borrower + reserve confirm a lot ─────────────────────────────
def test_multi_lot_missing_anchors_flagged():
    # two property lots, but only one reserve and one borrower
    ex = [E("property", property_type="flat", lot="1"),
          E("property", property_type="flat", lot="2"),
          E("auction_terms", reserve_price_num="1000000", lot="1"),
          E("borrower", role="borrower", lot="1")]
    c = _codes(ex)
    assert "lot_missing_reserve" in c
    assert "lot_missing_borrower" in c


def test_single_lot_does_not_trigger_anchor_deficit():
    ex = [E("property", property_type="flat", lot="1"),
          E("auction_terms", reserve_price_num="1000000", lot="1"),
          E("borrower", role="borrower", lot="1")]
    c = _codes(ex)
    assert "lot_missing_reserve" not in c
    assert "lot_missing_borrower" not in c
