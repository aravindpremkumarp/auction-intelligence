"""Tests for the readiness score + have/missing checklist (pure logic)."""
from __future__ import annotations

from api.dossier import taxonomy as tax
from api.dossier.checklist import build_checklist, readiness_score


def test_score_empty_dossier_is_zero() -> None:
    s = readiness_score(set())
    assert s == {"score": 0, "have": 0, "total": 10}


def test_score_all_uploadable_minimums_is_100() -> None:
    present: set[str] = set()
    for item in tax.MINIMUM_SET:
        if item.uploadable:
            present.add(item.doc_type_ids[0])  # satisfy with the first option
    s = readiness_score(present)
    assert s == {"score": 100, "have": 10, "total": 10}


def test_score_is_ten_points_per_item() -> None:
    present = {"sale_deed", "patta", "encumbrance_certificate"}
    s = readiness_score(present)
    assert s["have"] == 3
    assert s["score"] == 30


def test_layout_item_satisfied_by_any_approval_type() -> None:
    # Item 12 is satisfied by building_plan_approval even though it isn't the
    # first option in the tuple.
    assert readiness_score({"building_plan_approval"})["have"] == 1
    assert readiness_score({"planning_permission"})["have"] == 1


def test_advisory_items_never_affect_score() -> None:
    # The two Phase-2 outputs present should not move the score.
    present = {"advocate_legal_opinion", "court_case_search_report"}
    assert readiness_score(present)["score"] == 0


def test_extra_non_minimum_docs_do_not_affect_score() -> None:
    assert readiness_score({"chitta", "electricity_bill", "uds_details"})["score"] == 0


def test_build_checklist_structure() -> None:
    cl = build_checklist({"sale_deed", "patta"})
    assert cl["score"] == {"score": 20, "have": 2, "total": 10}
    assert len(cl["minimum_set"]) == 12
    assert len(cl["categories"]) == 9

    # Present items are flagged; missing uploadable items carry a go-get-it link.
    by_label = {m["label"]: m for m in cl["minimum_set"]}
    assert by_label["Sale Deed"]["present"] is True
    ec = by_label["Encumbrance Certificate"]
    assert ec["present"] is False
    assert ec["go_get_it"]["url"]


def test_build_checklist_missing_minimum_excludes_present_and_advisory() -> None:
    cl = build_checklist({"sale_deed"})
    labels = {m["label"] for m in cl["missing_minimum"]}
    # Present must-have excluded.
    assert "Sale Deed" not in labels
    # Advisory (non-uploadable) excluded from the actionable missing list.
    assert "Advocate Legal Opinion" not in labels
    assert "Court Case Search Report" not in labels
    # A genuine missing uploadable must-have is present.
    assert "Patta" in labels
    # 9 uploadable must-haves remain missing (10 uploadable - 1 present).
    assert len(cl["missing_minimum"]) == 9


def test_category_counts_reflect_present_docs() -> None:
    cl = build_checklist({"patta", "fmb_sketch"})
    cats = {c["id"]: c for c in cl["categories"]}
    # Both Patta and FMB are category C (Revenue Records).
    assert cats["C"]["have"] == 2
    assert cats["A"]["have"] == 0
