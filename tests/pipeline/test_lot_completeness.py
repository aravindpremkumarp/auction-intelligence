"""Lot-level completeness of an extraction: recall, then the four fields.

DB-free — every function under test is pure. The entity shape is the stored one
(Document.extraction_json: {cls, text, attrs}).
"""
from __future__ import annotations

from pipeline.lot_completeness import (
    build_report, doc_completeness, lot_rows, rollup, worst_documents,
)


def E(cls, text="x", **attrs):
    return {"cls": cls, "text": text, "attrs": attrs}


def _full_lot(li="1"):
    return [
        E("full_description", "the whole schedule ...", lot_index=li),
        E("property", "vacant land", property_type="land", lot_index=li),
        E("location", "situated at ...", village="Puliyur", taluk="Egmore",
          district="Chennai", lot_index=li),
        E("extent", "2400 sq.ft.", total_area="2400", lot_index=li),
    ]


# ── lots, and what counts as one ─────────────────────────────────────────────

def test_a_lot_with_everything_has_no_gaps():
    d = doc_completeness(_full_lot(), expected_lot_count=1)
    assert d["extracted_lots"] == 1 and d["lot_delta"] == 0
    assert d["missing_full_description"] == [] and d["missing_place"] == []
    assert d["missing_extent"] == [] and d["missing_property_type"] == []


def test_notice_level_classes_do_not_invent_a_lot():
    ents = [E("full_terms", "terms ..."), E("emd_account", "acct"),
            E("contact", "phone")] + _full_lot("2")
    rows = lot_rows(ents)
    assert list(rows) == ["2"]


def test_lot_index_1_and_the_string_1_are_one_lot():
    ents = [E("property", property_type="flat", lot_index=1),
            E("extent", "800 sq.ft.", lot_index="1")]
    assert list(lot_rows(ents)) == ["1"]


# ── 1. expected vs extracted ─────────────────────────────────────────────────

def test_under_recall_is_counted_against_the_confirmed_count():
    d = doc_completeness(_full_lot("1") + _full_lot("2"), expected_lot_count=5)
    assert d["extracted_lots"] == 2 and d["lot_delta"] == -3


def test_an_unknown_expected_count_is_not_a_match():
    d = doc_completeness(_full_lot(), expected_lot_count=None)
    assert d["lot_delta"] is None
    t = rollup([d])
    assert t["documents_with_expected_count"] == 0
    assert t["documents_matching_expected"] == 0
    assert t["expected_lots"] == 0
    # the lot still counts for the field gaps
    assert t["extracted_lots"] == 1


def test_rollup_splits_shortfall_from_overshoot():
    docs = [doc_completeness(_full_lot("1"), expected_lot_count=3),
            doc_completeness(_full_lot("1") + _full_lot("2"), expected_lot_count=1)]
    t = rollup(docs)
    assert t["expected_lots"] == 4
    assert t["extracted_lots_where_expected_known"] == 3
    assert (t["documents_under_expected"], t["documents_over_expected"]) == (1, 1)
    assert (t["lots_short_of_expected"], t["lots_beyond_expected"]) == (2, 1)


# ── 2. full_description ──────────────────────────────────────────────────────

def test_a_lot_with_no_full_description_block_is_flagged():
    ents = [e for e in _full_lot() if e["cls"] != "full_description"]
    assert doc_completeness(ents)["missing_full_description"] == ["1"]


def test_an_empty_full_description_span_does_not_count_as_one():
    ents = [E("full_description", "", lot_index="1"),
            E("property", property_type="land", lot_index="1")]
    assert doc_completeness(ents)["missing_full_description"] == ["1"]


# ── 3. village / taluk / district ────────────────────────────────────────────

def test_each_missing_place_field_is_reported_separately():
    ents = [E("location", "situated at ...", village="Puliyur", lot_index="1")]
    d = doc_completeness(ents)
    assert d["missing_village"] == []
    assert d["missing_taluk"] == ["1"] and d["missing_district"] == ["1"]
    assert d["missing_place"] == ["1"]


def test_hobli_stands_in_for_taluk():
    ents = [E("location", "Kasaba Hobli, Davangere", village="Karur",
              hobli="Kasaba", district="Davangere", lot_index="1")]
    d = doc_completeness(ents)
    assert d["missing_taluk"] == [] and d["missing_place"] == []


def test_registration_sub_district_is_counted_apart_from_taluk():
    ents = [E("location", "SRO Madhavaram", village="Madhavaram",
              district="Chennai", registration_sub_district="Madhavaram",
              lot_index="1")]
    d = doc_completeness(ents)
    assert d["missing_registration_sub_district"] == []
    assert d["missing_taluk"] == ["1"]          # an SRO is not a revenue taluk


def test_place_fields_on_the_property_block_still_count():
    ents = [E("property", "land at Perambalur", property_type="land",
              village="Perambalur North", taluk="Perambalur",
              district="Perambalur", lot_index="1")]
    assert doc_completeness(ents)["missing_place"] == []


def test_null_like_values_are_not_a_place():
    ents = [E("location", "?", village="N/A", taluk="null", district="",
              lot_index="1")]
    d = doc_completeness(ents)
    assert d["missing_village"] == ["1"] and d["missing_taluk"] == ["1"]
    assert d["missing_district"] == ["1"]


# ── 4. extent ────────────────────────────────────────────────────────────────

def test_a_lot_with_no_extent_entity_is_flagged():
    ents = [e for e in _full_lot() if e["cls"] != "extent"]
    assert doc_completeness(ents)["missing_extent"] == ["1"]


def test_an_extent_span_with_no_filled_attribute_still_counts():
    ents = [E("extent", "2 acres 30 cents", lot_index="1")]
    assert doc_completeness(ents)["missing_extent"] == []


def test_an_undivided_share_counts_as_an_extent():
    ents = [E("extent", "UDS 430 sq.ft.", undivided_share="430", lot_index="1")]
    assert doc_completeness(ents)["missing_extent"] == []


# ── 5. property_type ─────────────────────────────────────────────────────────

def test_a_property_block_with_no_type_is_flagged():
    ents = [E("property", "a building", lot_index="1")]
    assert doc_completeness(ents)["missing_property_type"] == ["1"]


def test_a_lot_with_no_property_block_at_all_is_flagged():
    ents = [E("extent", "2400 sq.ft.", lot_index="1")]
    assert doc_completeness(ents)["missing_property_type"] == ["1"]


# ── the report ───────────────────────────────────────────────────────────────

def test_percentages_are_over_extracted_lots():
    rows = [{"aid": "a", "expected_lot_count": 2,
             "entities": _full_lot("1") + [E("property", property_type="flat",
                                             lot_index="2")]}]
    t = build_report(rows)["totals"]
    assert t["extracted_lots"] == 2
    assert t["lot_gaps"]["missing_extent"] == 1
    assert t["lot_gaps_pct"]["missing_extent"] == 50.0


def test_an_empty_extraction_is_counted_as_a_document_with_no_lots():
    docs = [doc_completeness([], expected_lot_count=40, aid="empty"),
            doc_completeness(_full_lot(), expected_lot_count=1, aid="ok")]
    t = rollup(docs)
    assert t["documents_with_no_lots"] == 1
    assert t["lots_short_of_expected"] == 40
    # it has no lots, so it contributes no field gaps — the queue must still show it
    assert [d["aid"] for d in worst_documents(docs)] == ["empty"]


def test_a_clean_document_is_not_in_the_review_queue():
    clean = doc_completeness(_full_lot(), expected_lot_count=1, aid="clean")
    broken = doc_completeness([E("property", lot_index="1")],
                              expected_lot_count=4, aid="broken")
    assert [d["aid"] for d in worst_documents([clean, broken])] == ["broken"]
