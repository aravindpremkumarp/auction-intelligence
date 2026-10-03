"""The reader's form: verbatim quotes, lenient enums, a rendered catalogue."""
from __future__ import annotations

import json

import pytest

from pipeline.reader import schema as S


def test_lot_read_round_trips_json():
    lot = S.LotRead(
        lot_label="1",
        reserve_price=S.Money(status="found", quote="Rs.70.00 Lakhs", unit="lakh"),
        emd=S.Money(status="found", quote="Rs.7,00,000/-"),
        dates=[S.DateFact(status="found", quote="08-05-2026 at 11.00 AM",
                          event="auction_start", iso="2026-05-08T11:00")],
        location=S.Place(quote="Kolathuvanchery Village, Kundrathur Taluk",
                         village="Kolathuvanchery", taluk="Kundrathur"),
        identifiers=[S.Identifier(kind="survey_old", value="72/1B", quote="S.F.No.72/1B")],
        extents=[S.Extent(role="built_up_area", quote="1987.50 sq.ft")],
        boundaries=[S.Boundary(side="north", adjacency_quote="Road", measurement_quote="40 Feet")],
        description=S.Anchors(first_words="All that piece and parcel", last_words="Sub Registration District of Kundrathur"),
    )
    again = S.LotRead.model_validate_json(lot.model_dump_json())
    assert again == lot
    assert again.reserve_price.unit == "lakh"


def test_enums_are_lenient_on_input():
    lot = S.LotRead.model_validate({"possession_type": "Physical",
                                    "reserve_price": {"status": "FOUND", "quote": "x", "unit": "Lakhs"}})
    assert lot.possession_type == "physical"
    assert lot.reserve_price.status == "found"
    assert lot.reserve_price.unit is None          # "Lakhs" is not an enum value: dropped, not crashed
    assert S.Extent.model_validate({"role": "Built Up Area", "quote": "1 sq.ft"}).role == "built_up_area"
    assert S.Boundary.model_validate({"side": "NORTH"}).side == "north"
    assert S.NoticeRead.model_validate({"legal_basis": "sarfaesi"}).legal_basis == "SARFAESI"


def test_unknown_fields_are_ignored_and_extras_capped():
    lot = S.LotRead.model_validate({"surprise": 1, "extras": [
        {"key": f"k{i}", "value_quote": "v"} for i in range(8)]})
    assert len(lot.extras) == 5


def test_key_fact_defaults_to_not_stated():
    assert S.LotRead().reserve_price.status == "not_stated"
    assert S.LotRead().possession.quote is None


def test_catalogue_names_every_field_with_a_description_on_the_judgement_calls():
    cat = S.render_catalogue()
    for path in S.all_field_paths():
        model, fld = path.split(".")
        assert f"  - {fld} (" in cat, f"{path} missing from catalogue"
    # The fields that carry a rule must say it.
    for must in ("not_stated", "Never infer", "uds_parent_extent", "verbatim", "first 4"):
        assert must in cat
    assert "SINGLE JSON object" not in cat and "MinerU" not in cat


def test_strict_json_schema_forbids_extras_everywhere():
    sch = S.strict_json_schema(S.SegmentRead)
    seen = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "object" and "properties" in n:
                seen.append(n)
                assert n["additionalProperties"] is False
                assert set(n["required"]) == set(n["properties"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(sch)
    assert len(seen) > 5
    json.dumps(sch)          # serialisable


def test_schema_version_is_int():
    assert isinstance(S.SCHEMA_VERSION, int) and S.SCHEMA_VERSION >= 1
