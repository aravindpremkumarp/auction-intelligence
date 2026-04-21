"""Tests for scripts/link_reauctions — the re-auction matcher. These cover
the pure Python pieces (total-area parser, pair finder, union-find
expansion) without hitting Neo4j."""
from __future__ import annotations

import pytest


def test_parse_total_area_sqft_basic_units() -> None:
    from scripts.link_reauctions import parse_total_area_sqft

    assert parse_total_area_sqft("1295 Sq Ft") == pytest.approx(1295.0)
    assert parse_total_area_sqft("1,295 sqft") == pytest.approx(1295.0)
    assert parse_total_area_sqft("120 sq m") == pytest.approx(1291.67, rel=0.01)
    assert parse_total_area_sqft("0.5 acre") == pytest.approx(21780.0)
    assert parse_total_area_sqft("3 cents") == pytest.approx(1306.8)
    # Bare large number falls back to sq ft.
    assert parse_total_area_sqft("1295") == pytest.approx(1295.0)


def test_parse_total_area_rejects_unparseable_small_bare_numbers() -> None:
    from scripts.link_reauctions import parse_total_area_sqft

    # "5" with no unit could be 5 cents, 5 sq m, 5 sq ft — too ambiguous to
    # match against. Better to skip than guess.
    assert parse_total_area_sqft("5") is None
    assert parse_total_area_sqft("") is None
    assert parse_total_area_sqft(None) is None
    assert parse_total_area_sqft("no numbers here") is None


def test_areas_agree_within_tolerance() -> None:
    from scripts.link_reauctions import areas_agree

    assert areas_agree(1000.0, 1050.0) is True   # 5% diff
    assert areas_agree(1000.0, 1099.0) is True   # 9.9% diff
    assert areas_agree(1000.0, 1200.0) is False  # 20% diff
    assert areas_agree(None, 1000.0) is False
    assert areas_agree(1000.0, 0.0) is False


def test_survey_number_rule_requires_borrower_or_location() -> None:
    from scripts.link_reauctions import find_reauction_pairs

    # Shared survey number but neither borrower nor city+area align —
    # ignore (survey numbers alone are not unique across districts).
    auctions = [
        {
            "auction_id": "A", "borrower": "Alice", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1000 sqft",
            "survey_numbers": [{"survey_no": "10", "subdivision": "1"}],
        },
        {
            "auction_id": "B", "borrower": "Bob", "bank": "SBI",
            "city": "Kanchipuram", "area": "Kancheepuram", "total_area": "1000 sqft",
            "survey_numbers": [{"survey_no": "10", "subdivision": "1"}],
        },
    ]
    assert find_reauction_pairs(auctions) == []


def test_survey_number_rule_fires_on_borrower_match() -> None:
    from scripts.link_reauctions import find_reauction_pairs

    auctions = [
        {
            "auction_id": "A", "borrower": "Alice", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1000 sqft",
            "survey_numbers": [{"survey_no": "10", "subdivision": "1"}],
        },
        {
            "auction_id": "B", "borrower": "Alice", "bank": "Canara",
            "city": "Chennai", "area": "Other Area", "total_area": "1100 sqft",
            "survey_numbers": [{"survey_no": "10", "subdivision": "1"}],
        },
    ]
    pairs = find_reauction_pairs(auctions)
    assert len(pairs) == 1
    a, b, reason, conf = pairs[0]
    assert {a, b} == {"A", "B"}
    assert reason == "survey_number"
    assert conf == "high"


def test_survey_number_rule_fires_on_city_and_area_match() -> None:
    from scripts.link_reauctions import find_reauction_pairs

    auctions = [
        {
            "auction_id": "A", "borrower": "Alice", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1000 sqft",
            "survey_numbers": [{"survey_no": "42", "subdivision": "A"}],
        },
        {
            "auction_id": "B", "borrower": "Bob", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1000 sqft",
            "survey_numbers": [{"survey_no": "42", "subdivision": "A"}],
        },
    ]
    pairs = find_reauction_pairs(auctions)
    assert len(pairs) == 1
    assert pairs[0][2] == "survey_number"


def test_borrower_location_rule() -> None:
    from scripts.link_reauctions import find_reauction_pairs

    auctions = [
        {
            "auction_id": "A", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1000 sqft",
            "survey_numbers": [],
        },
        {
            "auction_id": "B", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1050 sqft",
            "survey_numbers": [],
        },
    ]
    pairs = find_reauction_pairs(auctions)
    assert len(pairs) == 1
    a, b, reason, conf = pairs[0]
    assert {a, b} == {"A", "B"}
    assert reason == "borrower_location"
    assert conf == "medium"


def test_borrower_location_rule_rejects_differing_area() -> None:
    from scripts.link_reauctions import find_reauction_pairs

    # Borrower/bank/area match, but total_area differs by 30% — reject.
    auctions = [
        {
            "auction_id": "A", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1000 sqft",
            "survey_numbers": [],
        },
        {
            "auction_id": "B", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1400 sqft",
            "survey_numbers": [],
        },
    ]
    assert find_reauction_pairs(auctions) == []


def test_expand_clusters_is_transitive() -> None:
    """A↔B via survey and B↔C via borrower+location should produce an
    A↔C pair too (same property, three auction rounds)."""
    from scripts.link_reauctions import find_reauction_pairs, expand_clusters

    auctions = [
        {
            "auction_id": "A", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1000 sqft",
            "survey_numbers": [{"survey_no": "10", "subdivision": "1"}],
        },
        {
            "auction_id": "B", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1000 sqft",
            "survey_numbers": [{"survey_no": "10", "subdivision": "1"}],
        },
        {
            "auction_id": "C", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1020 sqft",
            "survey_numbers": [],
        },
    ]
    pairs = find_reauction_pairs(auctions)
    clusters, expanded = expand_clusters(auctions, pairs)

    assert len(clusters) == 1
    assert set(clusters[0]) == {"A", "B", "C"}
    # 3 nodes → C(3,2) = 3 pairs.
    assert len(expanded) == 3
    expanded_pairs = {(a, b) for a, b, _, _ in expanded}
    assert expanded_pairs == {("A", "B"), ("A", "C"), ("B", "C")}


def test_expand_clusters_lone_auction_produces_no_pairs() -> None:
    from scripts.link_reauctions import find_reauction_pairs, expand_clusters

    auctions = [
        {
            "auction_id": "A", "borrower": "Alice", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1000 sqft",
            "survey_numbers": [],
        },
    ]
    pairs = find_reauction_pairs(auctions)
    clusters, expanded = expand_clusters(auctions, pairs)
    assert clusters == []
    assert expanded == []
