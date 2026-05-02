"""Tests for `match_pasted_listing` — the tool that finds an auction in the
graph from a pasted property blurb (WhatsApp forward, broker note, bank
circular). Covers: structured field extraction, structured Cypher gating
on price ± date ± area, confidence scoring, and graceful low-confidence
fallback so the LLM never presents a poor match as the "best match".
"""
from __future__ import annotations

from datetime import date

import pytest


# ── Extraction unit tests ────────────────────────────────────────────────

def test_extract_price_lakhs() -> None:
    from api.tools.cypher_tools import _extract_listing_fields

    out = _extract_listing_fields("Reserve Price: 32 lakhs")
    assert out.reserve_price == 3_200_000


def test_extract_price_decimal_lakhs() -> None:
    from api.tools.cypher_tools import _extract_listing_fields

    out = _extract_listing_fields("Reserve Price: 31.5 lakhs")
    assert out.reserve_price == 3_150_000


def test_extract_price_crore() -> None:
    from api.tools.cypher_tools import _extract_listing_fields

    out = _extract_listing_fields("Reserve Price: 1.5 crore")
    assert out.reserve_price == 15_000_000


def test_extract_price_rupee_symbol_with_commas() -> None:
    from api.tools.cypher_tools import _extract_listing_fields

    out = _extract_listing_fields("Reserve: ₹32,00,000")
    assert out.reserve_price == 3_200_000


def test_extract_price_rs_with_commas() -> None:
    from api.tools.cypher_tools import _extract_listing_fields

    out = _extract_listing_fields("Reserve Price Rs. 32,00,000")
    assert out.reserve_price == 3_200_000


def test_extract_price_short_l_suffix() -> None:
    from api.tools.cypher_tools import _extract_listing_fields

    out = _extract_listing_fields("Reserve 32L EMD 3.2L")
    # First match wins as reserve; lakh → 32 lakh = 3,200,000
    assert out.reserve_price == 3_200_000


def test_extract_dates_dd_mm_yyyy() -> None:
    from api.tools.cypher_tools import _extract_listing_fields

    out = _extract_listing_fields("EMD Date: 24/05/2026  Auction: 25/05/2026")
    # Indian convention is DD/MM/YYYY.
    assert out.auction_date == date(2026, 5, 25)
    assert out.emd_date == date(2026, 5, 24)


def test_extract_pin_code() -> None:
    from api.tools.cypher_tools import _extract_listing_fields

    out = _extract_listing_fields("Poonamallee Chennai – 600056")
    assert out.pin == "600056"


def test_extract_built_up_area_and_uds() -> None:
    from api.tools.cypher_tools import _extract_listing_fields

    out = _extract_listing_fields("Built up area: 741 Sqft  UDS: 331 sqft")
    assert out.built_up_sqft == 741
    assert out.uds_sqft == 331


def test_extract_known_city_and_area(monkeypatch) -> None:
    """City and area must be matched against the cached graph vocabulary
    so we do NOT mistake a building name ("Sai Nila") for an area."""
    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "_load_known_locations", lambda: (
        {"Chennai", "Coimbatore"},
        {"Poonamallee", "Ambattur", "Sriperumbudur"},
    ))

    out = ct._extract_listing_fields(
        "Sai Nila Flats, Plot No.46, Balaraman Nagar, Poonamallee Chennai – 600056"
    )
    assert out.city == "Chennai"
    assert out.area == "Poonamallee"


def test_full_paste_extraction(monkeypatch) -> None:
    """Full Sai Nila Flats paste — every structured field surfaces."""
    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "_load_known_locations", lambda: (
        {"Chennai"},
        {"Poonamallee"},
    ))

    paste = (
        "🏦BANK E-AUCTION PROPERTY🏦\n"
        "👉POONAMALLEE\n"
        "Residential flat\n"
        "Flat No S1, Second Floor, Sai Nila Flats\n"
        "Plot No.46, Balaraman Nagar, Poonamallee Chennai – 600056\n"
        "Built up area: 741 Sqft\nUDS: 331 sqft\n2BHK Flat\n"
        "Reserve Price: 32 lakhs\n"
        "EMD Date:24/05/2026\nAuction: 25/05/2026"
    )
    out = ct._extract_listing_fields(paste)
    assert out.reserve_price == 3_200_000
    assert out.auction_date == date(2026, 5, 25)
    assert out.emd_date == date(2026, 5, 24)
    assert out.pin == "600056"
    assert out.city == "Chennai"
    assert out.area == "Poonamallee"
    assert out.built_up_sqft == 741
    assert out.uds_sqft == 331


# ── Match orchestration tests ────────────────────────────────────────────

def _patch_run_query(monkeypatch, rows: list[dict]):
    """Stub run_query to capture cypher/params and return canned rows."""
    calls: list[tuple[str, dict]] = []

    def fake_run_query(cypher: str, params: dict | None = None):
        calls.append((cypher, dict(params or {})))
        return list(rows)

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_query", fake_run_query)
    monkeypatch.setattr(ct, "_load_known_locations", lambda: (
        {"Chennai"}, {"Poonamallee", "Ambattur"},
    ))
    return calls


def test_match_runs_structured_cypher_first(monkeypatch) -> None:
    """The Sai Nila paste must produce a Cypher that filters on price
    (±2%), auction date (±2 days), and area — together, in one query."""
    rows = [{
        "auction_id": "750879",
        "title": "Bajaj Housing Finance Ltd – Poonamallee",
        "url": "https://www.eauctionsindia.com/properties/750879",
        "reserve_price": 3_200_000,
        "auction_start": "2026-05-25T11:00:00",
        "city": "Chennai", "area": "Poonamallee",
        "bank": "Bajaj Housing Finance Ltd",
        "description": "Sai Nila Flats, Plot No. 46, Balaraman Nagar...",
    }]
    calls = _patch_run_query(monkeypatch, rows)

    from api.tools.cypher_tools import match_pasted_listing
    paste = (
        "Reserve Price: 32 lakhs  EMD Date:24/05/2026  Auction: 25/05/2026\n"
        "Sai Nila Flats, Plot No.46, Balaraman Nagar, Poonamallee Chennai – 600056"
    )
    out = match_pasted_listing(paste)

    assert calls, "match_pasted_listing must call run_query at least once"
    cypher, params = calls[0]
    # Price band: ±2% around 32 lakhs
    assert params["min_price"] == pytest.approx(3_200_000 * 0.98)
    assert params["max_price"] == pytest.approx(3_200_000 * 1.02)
    assert "a.reserve_price_num >= $min_price" in cypher
    assert "a.reserve_price_num <= $max_price" in cypher
    # Date band: ±2 days around auction date
    assert "a.auction_start_dt >= $starts_after" in cypher
    assert "a.auction_start_dt <= $starts_before" in cypher
    assert params["starts_after"].startswith("2026-05-23")
    assert params["starts_before"].startswith("2026-05-27")
    # Area + city
    assert "(a)-[:LOCATED_IN_AREA]->(ar:Area)" in cypher
    assert "(a)-[:LOCATED_IN_CITY]->(c:City {name: $city})" in cypher
    assert params["area"] == "Poonamallee"
    assert params["city"] == "Chennai"

    assert out["match"]["auction_id"] == "750879"
    assert out["confidence"] >= 0.8


def test_match_returns_high_confidence_when_3_fields_match(monkeypatch) -> None:
    rows = [{"auction_id": "750879", "reserve_price": 3_200_000,
             "auction_start": "2026-05-25T11:00:00", "city": "Chennai",
             "area": "Poonamallee"}]
    _patch_run_query(monkeypatch, rows)

    from api.tools.cypher_tools import match_pasted_listing
    out = match_pasted_listing(
        "Reserve 32 lakhs  Auction 25/05/2026  Poonamallee Chennai – 600056"
    )
    # 4 strong signals matched: price, date, area, pin
    assert out["confidence"] >= 0.8
    assert out["match"]["auction_id"] == "750879"


def test_match_low_confidence_when_no_structured_match(monkeypatch) -> None:
    """Empty structured candidates → fall back to vector search → low confidence
    must be flagged so the LLM does NOT call any returned alternates the
    'best match'."""
    _patch_run_query(monkeypatch, [])

    from api.tools.cypher_tools import match_pasted_listing
    out = match_pasted_listing(
        "Reserve 32 lakhs  Auction 25/05/2026  Poonamallee Chennai – 600056"
    )
    assert out["match"] is None
    assert out["confidence"] < 0.6
    assert "alternates" in out


def test_match_returns_alternates_when_multiple_structured_hits(monkeypatch) -> None:
    """Multiple candidates in the price/date/area window — all surface as
    alternates so the user can sanity-check; top-1 still gets `match`."""
    rows = [
        {"auction_id": "750879", "reserve_price": 3_200_000,
         "auction_start": "2026-05-25T11:00:00", "city": "Chennai",
         "area": "Poonamallee",
         "description": "Sai Nila Flats, Plot No. 46, Balaraman Nagar"},
        {"auction_id": "750880", "reserve_price": 3_180_000,
         "auction_start": "2026-05-25T11:00:00", "city": "Chennai",
         "area": "Poonamallee",
         "description": "Different building, same locality"},
    ]
    _patch_run_query(monkeypatch, rows)

    from api.tools.cypher_tools import match_pasted_listing
    out = match_pasted_listing(
        "Sai Nila Flats Plot No.46 Reserve 32 lakhs Auction 25/05/2026 Poonamallee Chennai 600056"
    )
    assert out["match"]["auction_id"] == "750879"  # tie-broken by token overlap
    assert len(out["alternates"]) >= 1
    alt_ids = {a["auction_id"] for a in out["alternates"]}
    assert "750880" in alt_ids


def test_match_extracted_fields_are_returned(monkeypatch) -> None:
    """The `extracted` block lets the LLM (and the user) see what we parsed
    out of the paste — important for transparency when confidence is low."""
    _patch_run_query(monkeypatch, [])

    from api.tools.cypher_tools import match_pasted_listing
    out = match_pasted_listing(
        "Reserve Price: 32 lakhs  Auction: 25/05/2026  Poonamallee Chennai – 600056"
    )
    extracted = out["extracted"]
    assert extracted["reserve_price"] == 3_200_000
    assert extracted["auction_date"] == "2026-05-25"
    assert extracted["pin"] == "600056"
    assert extracted["area"] == "Poonamallee"
    assert extracted["city"] == "Chennai"


# ── Progressive widening tests ────────────────────────────────────────────
# The user's explicit ask: "if we dont find any properties based on provided
# details then we should atleast show close recommendation". When the strict
# (price+date+area+city) Cypher returns nothing, we must NOT bail with an
# empty alternates list — we widen and surface what we found, with an
# explicit `widening_reason` so the LLM can tell the user which constraint
# was relaxed.

def _patch_tiered_run_query(monkeypatch, tiers: list[list[dict]]):
    """Stub run_query that returns a different row set per call. Lets a
    test simulate the progressive-widening cascade: tier 0 = strict query,
    tier 1+ = each widening tier in order."""
    calls: list[tuple[str, dict]] = []
    state = {"i": 0}

    def fake_run_query(cypher: str, params: dict | None = None):
        calls.append((cypher, dict(params or {})))
        i = state["i"]
        state["i"] += 1
        return list(tiers[i]) if i < len(tiers) else []

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_query", fake_run_query)
    monkeypatch.setattr(ct, "_load_known_locations", lambda: (
        {"Chennai"}, {"Poonamallee", "Ambattur"},
    ))
    return calls


def test_widens_when_strict_returns_nothing(monkeypatch) -> None:
    """Strict price+date+area returns []. Widening drops the date and finds
    a sibling Poonamallee flat — that surfaces as a candidate with an
    explanatory `widening_reason`, NOT an empty alternates list."""
    sibling = {
        "auction_id": "999111",
        "title": "Other Poonamallee flat",
        "reserve_price": 3_200_000,
        "auction_start": "2026-07-15T11:00:00",  # different month
        "city": "Chennai", "area": "Poonamallee",
        "description": "Different building, same locality and price",
    }
    # tier 0 (strict): empty. tier 1 (date dropped): sibling.
    calls = _patch_tiered_run_query(monkeypatch, [[], [sibling]])

    from api.tools.cypher_tools import match_pasted_listing
    out = match_pasted_listing(
        "Reserve Price: 32 lakhs  Auction: 25/05/2026  Poonamallee Chennai – 600056"
    )

    assert out["match"] is None  # no high-confidence exact match
    assert out["confidence"] == 0.0
    assert out["candidates"], "must surface CLOSE candidates, never empty"
    assert out["candidates"][0]["auction_id"] == "999111"
    assert out["widening_reason"] is not None
    assert "date" in out["widening_reason"].lower()
    # Two run_query calls: strict + first widening tier.
    assert len(calls) == 2


def test_widens_through_multiple_tiers_until_hits(monkeypatch) -> None:
    """Strict empty, drop-date empty, widen-price empty, drop-area finds.
    The first non-empty tier wins and its `widening_reason` is reported."""
    cousin = {
        "auction_id": "888222",
        "title": "Different area Chennai flat",
        "reserve_price": 3_500_000,
        "auction_start": "2026-09-10T11:00:00",
        "city": "Chennai", "area": "Ambattur",
        "description": "Other area, similar price band",
    }
    calls = _patch_tiered_run_query(monkeypatch, [
        [],         # tier 0 strict
        [],         # tier 1 (drop date)
        [],         # tier 2 (drop date + widen price ±10%)
        [cousin],   # tier 3 (drop date + widen price + drop area)
    ])

    from api.tools.cypher_tools import match_pasted_listing
    out = match_pasted_listing(
        "Reserve Price: 32 lakhs  Auction: 25/05/2026  Poonamallee Chennai – 600056"
    )
    assert out["candidates"][0]["auction_id"] == "888222"
    assert out["widening_reason"] is not None
    assert "area" in out["widening_reason"].lower()
    assert len(calls) == 4


def test_widens_returns_empty_only_when_even_loosest_tier_misses(monkeypatch) -> None:
    """When even the city-only tier returns nothing — paste mentions a city
    we don't have any data for — `candidates` is genuinely empty, but
    `extracted` still carries what we parsed so the LLM can ask for help."""
    calls = _patch_tiered_run_query(monkeypatch, [[], [], [], [], []])

    from api.tools.cypher_tools import match_pasted_listing
    out = match_pasted_listing(
        "Reserve Price: 32 lakhs  Auction: 25/05/2026  Poonamallee Chennai – 600056"
    )
    assert out["match"] is None
    assert out["candidates"] == []
    assert out["extracted"]["city"] == "Chennai"
    # 5 calls: strict + 4 widening tiers, all empty.
    assert len(calls) == 5


def test_widening_reason_is_None_when_strict_match_succeeds(monkeypatch) -> None:
    """Happy path: strict cypher hits; we never widen, so widening_reason
    must be None (not an empty string, not a 'no widening needed' note —
    None, so the LLM doesn't accidentally surface fallback framing)."""
    rows = [{
        "auction_id": "750879", "reserve_price": 3_200_000,
        "auction_start": "2026-05-25T11:00:00",
        "city": "Chennai", "area": "Poonamallee",
        "description": "Sai Nila Flats Plot 46",
    }]
    _patch_run_query(monkeypatch, rows)

    from api.tools.cypher_tools import match_pasted_listing
    out = match_pasted_listing(
        "Sai Nila Flats Plot 46 Reserve 32 lakhs Auction 25/05/2026 Poonamallee Chennai 600056"
    )
    assert out["match"]["auction_id"] == "750879"
    assert out["widening_reason"] is None
    assert out["candidates"][0]["auction_id"] == "750879"
