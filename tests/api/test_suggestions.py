"""Tests for api/chat/suggestions.py — the pure builder behind the data-driven
landing chips. It turns per-dimension `search_auctions(group_by=...)`
distributions into a varied, non-empty chip set; the router owns the Neo4j
calls and the TTL cache.

Loaded by file path (not `from api.chat.suggestions import ...`) so the test
stays dependency-free: importing the api.chat package pulls in the router and
its pydantic_ai/fastapi deps, which this module doesn't need."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SUGGEST_PY = Path(__file__).resolve().parents[2] / "api" / "chat" / "suggestions.py"
_spec = importlib.util.spec_from_file_location("suggestions_under_test", _SUGGEST_PY)
_suggest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_suggest)

build_suggestions = _suggest.build_suggestions


def _bucket(value: str, count: int) -> dict:
    return {"value": value, "auction_count": count}


# A realistic live snapshot: two cities, two property types, two categories,
# one area.
_RICH = {
    "city": [_bucket("Chennai", 412), _bucket("Coimbatore", 88)],
    "property_type": [_bucket("Flat", 230), _bucket("Plot", 140)],
    "asset_category": [_bucket("Residential", 500), _bucket("Commercial", 88)],
    "area": [_bucket("Ambattur", 14)],
}


def test_rich_data_yields_a_varied_four_chip_mix():
    chips = build_suggestions(_RICH)
    # _PICK_ORDER = city, property_type, asset_category, property_type, city, area
    # → the first four slots fill from city/type/category/type.
    assert chips == [
        {"label": "Auctions in Chennai", "q": "auctions in Chennai", "count": 412},
        {"label": "Flat listings", "q": "flat listings", "count": 230},
        {"label": "Residential properties", "q": "residential properties", "count": 500},
        {"label": "Plot listings", "q": "plot listings", "count": 140},
    ]


def test_every_chip_carries_label_q_and_count():
    for chip in build_suggestions(_RICH):
        assert set(chip) == {"label", "q", "count"}
        assert chip["label"] and chip["q"] and isinstance(chip["count"], int)


def test_empty_distributions_yield_no_chips():
    # The frontend reads [] as "keep the hardcoded fallback chips".
    assert build_suggestions({}) == []
    assert build_suggestions({"city": [], "property_type": []}) == []


def test_zero_and_negative_count_buckets_are_skipped():
    dists = {"city": [_bucket("Ghost Town", 0), _bucket("Chennai", 5)]}
    chips = build_suggestions(dists)
    assert [c["label"] for c in chips] == ["Auctions in Chennai"]


def test_thin_data_falls_through_to_available_buckets():
    # Only cities present: _PICK_ORDER visits "city" twice, so we get two
    # distinct city chips and nothing invented for the missing dimensions.
    dists = {"city": [_bucket("Chennai", 400), _bucket("Salem", 30), _bucket("Trichy", 12)]}
    chips = build_suggestions(dists)
    assert [c["label"] for c in chips] == ["Auctions in Chennai", "Auctions in Salem"]


def test_industrials_category_reads_as_industrial():
    dists = {"asset_category": [_bucket("Industrials", 30)]}
    chips = build_suggestions(dists)
    assert chips == [
        {"label": "Industrial properties", "q": "industrial properties", "count": 30},
    ]


def test_dedupe_by_question_text():
    # Two buckets whose questions collide (case-only difference) — the second
    # is dropped so the same chip never appears twice.
    dists = {"property_type": [_bucket("Flat", 20), _bucket("flat", 9), _bucket("Plot", 5)]}
    chips = build_suggestions(dists)
    labels = [c["label"] for c in chips]
    assert labels == ["Flat listings", "Plot listings"]


def test_max_chips_is_respected():
    chips = build_suggestions(_RICH, max_chips=2)
    assert len(chips) == 2
    assert chips[0]["label"] == "Auctions in Chennai"


def test_missing_or_malformed_count_is_skipped():
    dists = {"city": [{"value": "Chennai"}, _bucket("Madurai", 7)]}
    chips = build_suggestions(dists)
    # The count-less bucket is skipped (treated as 0); Madurai carries through.
    assert [c["label"] for c in chips] == ["Auctions in Madurai"]
