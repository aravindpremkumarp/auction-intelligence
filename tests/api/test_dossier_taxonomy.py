"""Integrity tests for the dossier document taxonomy.

These pin the contract the classifier and checklist both depend on: every
minimum-set item maps to a real doc type, ids are unique, and the score
denominator matches the design (10 uploadable must-haves in v1).
"""
from __future__ import annotations

from api.dossier import taxonomy as tax


def test_doc_type_ids_are_unique() -> None:
    ids = [d.id for d in tax.DOC_TYPES]
    assert len(ids) == len(set(ids))


def test_every_doc_type_has_a_valid_category() -> None:
    valid = {c.id for c in tax.CATEGORIES}
    for d in tax.DOC_TYPES:
        assert d.category in valid, f"{d.id} -> bad category {d.category}"


def test_minimum_set_references_real_doc_types() -> None:
    for item in tax.MINIMUM_SET:
        assert item.doc_type_ids, f"{item.label} has no doc types"
        for dt in item.doc_type_ids:
            assert dt in tax.ALL_DOC_TYPE_IDS, f"{item.label} -> unknown {dt}"


def test_minimum_set_shape_matches_design() -> None:
    # 12 items total; 10 uploadable (drive the score); 2 advisory (Phase-2).
    assert len(tax.MINIMUM_SET) == 12
    uploadable = [m for m in tax.MINIMUM_SET if m.uploadable]
    advisory = [m for m in tax.MINIMUM_SET if not m.uploadable]
    assert len(uploadable) == 10
    assert len(advisory) == 2
    assert tax.SCORABLE_ITEM_COUNT == 10
    # The two advisory items are the Phase-2 legal outputs.
    advisory_types = {dt for m in advisory for dt in m.doc_type_ids}
    assert advisory_types == {"advocate_legal_opinion", "court_case_search_report"}


def test_normalize_doc_type() -> None:
    assert tax.normalize_doc_type("sale_deed") == "sale_deed"
    assert tax.normalize_doc_type("SALE_DEED") == "sale_deed"
    assert tax.normalize_doc_type(None) is None
    assert tax.normalize_doc_type("") is None
    assert tax.normalize_doc_type("other") == tax.UNKNOWN_DOC_TYPE
    assert tax.normalize_doc_type("totally-made-up") == tax.UNKNOWN_DOC_TYPE


def test_present_doc_type_ids_filters_unknown_and_none() -> None:
    got = tax.present_doc_type_ids(["sale_deed", "unknown", None, "patta", "nope"])
    assert got == {"sale_deed", "patta"}


def test_render_taxonomy_for_prompt_lists_all_types() -> None:
    rendered = tax.render_taxonomy_for_prompt()
    for d in tax.DOC_TYPES:
        assert d.id in rendered
    for c in tax.CATEGORIES:
        assert c.label in rendered


def test_portal_link_specific_and_fallback() -> None:
    # Specific link for EC.
    ec = tax.portal_link_for("encumbrance_certificate")
    assert ec is not None and "tnreginet" in ec.url.lower()
    # Category fallback for a title deed (no per-type entry).
    deed = tax.portal_link_for("sale_deed")
    assert deed is not None and deed.how
    # Unknown id -> no link.
    assert tax.portal_link_for("totally-made-up") is None
