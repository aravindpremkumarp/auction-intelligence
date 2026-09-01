"""Row building for the property-taxonomy backfill: lot pairing, the explicit
UNKNOWN state, portal conflict, and the one-row-per-listing guarantee."""
from __future__ import annotations

import json

from scripts.backfill_property_type import build_rows


def _doc(filename, entities, listings, review_status=None):
    return {
        "filename": filename,
        "extraction_json": json.dumps(entities),
        "corrections_json": None,
        "review_status": review_status,
        "listings": listings,
    }


def _prop(ptype, lot="1"):
    return {"id": "0", "cls": "property", "text": ptype,
            "attrs": {"property_type": ptype, "lot_index": lot}}


def _terms(reserve, lot="1"):
    return {"id": "1", "cls": "auction_terms", "text": "",
            "attrs": {"reserve_price_num": reserve, "lot_index": lot}}


def test_single_lot_classifies_every_listing_it_backs():
    work = [_doc("n1.pdf", [_prop("vacant house site")],
                 [{"aid": "A1", "price": 100}])]
    rows, _ = build_rows(work, {})
    assert len(rows) == 1
    assert rows[0]["norm"] == "plot"
    assert rows[0]["raw"] == "vacant house site"
    assert rows[0]["source"] == "langextract"
    assert rows[0]["category"] == "residential"


def test_multi_lot_pairs_by_reserve_price():
    entities = [_prop("flat", lot="1"), _terms(500_000, lot="1"),
                _prop("agricultural land", lot="2"), _terms(900_000, lot="2")]
    work = [_doc("n2.pdf", entities,
                 [{"aid": "A1", "price": 900_000},
                  {"aid": "A2", "price": 500_000}])]
    rows, _ = build_rows(work, {})
    by_aid = {r["aid"]: r for r in rows}
    assert by_aid["A1"]["norm"] == "agricultural"
    assert by_aid["A2"]["norm"] == "flat"


def test_verified_review_marks_the_source_as_reviewer():
    work = [_doc("n3.pdf", [_prop("flat")], [{"aid": "A1", "price": 1}],
                 review_status="verified")]
    rows, _ = build_rows(work, {})
    assert rows[0]["source"] == "reviewer"


def test_lot_without_a_property_type_is_written_unknown_not_skipped():
    work = [_doc("n4.pdf", [_terms(100)], [{"aid": "A1", "price": 100}])]
    rows, stats = build_rows(work, {"A1": "Flat"})
    assert len(rows) == 1
    assert rows[0]["norm"] == "unknown"
    assert rows[0]["source"] == "none"
    assert rows[0]["raw"] is None
    assert stats["no_property_type_on_lot"] == 1


def test_portal_value_never_fills_the_gap():
    # the portal is confident it is a Flat; the notice said nothing
    work = [_doc("n5.pdf", [_terms(100)], [{"aid": "A1", "price": 100}])]
    rows, _ = build_rows(work, {"A1": "Flat"})
    assert rows[0]["portal"] == "Flat"
    # provenance stays honest: the notice named nothing, so neither does this
    assert rows[0]["norm"] == "unknown"
    # a gap is not a disagreement — and it is not agreement either, so the
    # verdict is null, which is what the scorecard counts as "never compared"
    assert rows[0]["conflict"] is None
    assert rows[0]["severity"] is None
    # ...but a type SEARCH still has to find this listing, so it falls back
    assert rows[0]["effective"] == "flat"


def test_conflict_flags_portal_default_against_the_notice():
    work = [_doc("n6.pdf", [_prop("land and building")],
                 [{"aid": "A1", "price": 100}])]
    rows, _ = build_rows(work, {"A1": "Plot"})
    assert rows[0]["norm"] == "house"
    assert rows[0]["conflict"] is True


def test_agreement_is_not_flagged():
    work = [_doc("n7.pdf", [_prop("flat")], [{"aid": "A1", "price": 100}])]
    rows, _ = build_rows(work, {"A1": "Flat"})
    assert rows[0]["conflict"] is False


def test_listing_linked_to_two_notices_yields_one_row():
    # same listing reached from an empty notice and a useful one
    work = [
        _doc("n8.pdf", [_terms(100)], [{"aid": "A1", "price": 100}]),
        _doc("n9.pdf", [_prop("flat"), _terms(100)],
             [{"aid": "A1", "price": 100}]),
    ]
    rows, stats = build_rows(work, {})
    assert len(rows) == 1
    assert rows[0]["norm"] == "flat"          # the informative row won
    assert stats["duplicate_rows_collapsed"] == 1


def test_reviewer_row_beats_langextract_row_for_the_same_listing():
    work = [
        _doc("n10.pdf", [_prop("plot"), _terms(100)],
             [{"aid": "A1", "price": 100}]),
        _doc("n11.pdf", [_prop("flat"), _terms(100)],
             [{"aid": "A1", "price": 100}], review_status="verified"),
    ]
    rows, _ = build_rows(work, {})
    assert len(rows) == 1
    assert rows[0]["source"] == "reviewer"
    assert rows[0]["norm"] == "flat"


def test_unpairable_listing_is_counted_not_written():
    # two lots, listing price matches neither -> cannot be paired
    entities = [_prop("flat", lot="1"), _terms(500, lot="1"),
                _prop("land", lot="2"), _terms(900, lot="2")]
    work = [_doc("n12.pdf", entities, [{"aid": "A1", "price": 12_345}])]
    rows, stats = build_rows(work, {})
    assert rows == []
    assert sum(v for k, v in stats.items() if k.startswith("unmatched_")) == 1


def test_empty_extraction_is_counted_not_written():
    work = [_doc("n13.pdf", [], [{"aid": "A1", "price": 100}])]
    rows, stats = build_rows(work, {})
    assert rows == []
    assert stats["empty_extraction"] == 1
