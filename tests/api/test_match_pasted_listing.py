"""Tests for `match_pasted_listing` v2 — finds an auction in the graph from
a pasted property blurb (WhatsApp forward, broker note, bank circular).

v2 anchors on **reserve_price ±2% AND auction_date ±2 days** as the primary
Cypher filter (no city, no area — those discriminate poorly because Tamil
Nadu auctions in Chennai's outer ring are filed under Tiruvallur or
Kanchipuram administrative districts but locals always say "Chennai").
Confidence is scored by counting how many INDEPENDENT signals from the
paste also appear in the matched property's `description`: built-up area
number, UDS number, distinctive locality tokens (e.g. "Balaraman Nagar"),
and plot/door numbers. The Sai Nila Flats canonical case (`auction_id
750879`) is the regression fixture.
"""
from __future__ import annotations

from datetime import date


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
    assert out.reserve_price == 3_200_000


def test_extract_dates_dd_mm_yyyy() -> None:
    from api.tools.cypher_tools import _extract_listing_fields
    out = _extract_listing_fields("EMD Date: 24/05/2026  Auction: 25/05/2026")
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


def test_extract_locality_tokens_picks_up_nagar_and_building() -> None:
    """Distinctive locality / building names — these are the strongest
    signals once price + date have narrowed the candidate set."""
    from api.tools.cypher_tools import _extract_listing_fields
    out = _extract_listing_fields(
        "Sai Nila Flats, Plot No.46, Balaraman Nagar, Poonamallee"
    )
    assert "balaraman nagar" in out.locality_tokens
    assert "sai nila flats" in out.locality_tokens


def test_extract_plot_no() -> None:
    from api.tools.cypher_tools import _extract_listing_fields
    out = _extract_listing_fields("Plot No.46, Balaraman Nagar")
    assert out.plot_no == "46"


def test_extract_known_city_and_area(monkeypatch) -> None:
    """City and area still get extracted (the LLM may want to display them
    in the `extracted` block), but they are NOT used as Cypher filters."""
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
    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "_load_known_locations", lambda: (
        {"Chennai"}, {"Poonamallee"},
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
    assert out.plot_no == "46"
    assert "balaraman nagar" in out.locality_tokens


# ── Match orchestration tests ────────────────────────────────────────────

def _patch_run_query(monkeypatch, rows: list[dict]):
    calls: list[tuple[str, dict]] = []

    def fake_run_query(cypher: str, params: dict | None = None):
        calls.append((cypher, dict(params or {})))
        return list(rows)

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_query", fake_run_query)
    monkeypatch.setattr(
        ct, "run_read_query",
        lambda cypher, params=None, timeout=10.0, max_rows=200: fake_run_query(cypher, params),
    )
    monkeypatch.setattr(ct, "_load_known_locations", lambda: (
        {"Chennai"}, {"Poonamallee", "Ambattur"},
    ))
    return calls


def test_primary_cypher_uses_only_price_and_date(monkeypatch) -> None:
    """v2's defining behavior: the primary Cypher filters on price+date
    ONLY. City and area are NOT in the filter — they discriminate poorly
    in greater Chennai (Tiruvallur/Kanchipuram districts that locals call
    Chennai)."""
    rows = [{
        "auction_id": "750879", "title": "Bajaj Poonamallee",
        "reserve_price": 3_200_000,
        "auction_start": "2026-05-25T11:00:00",
        "city": "Tiruvallur", "area": "Poonamallee taluk",
        "description": "Sai Nila Flats Plot No 46 Balaraman Nagar 741 sq.ft 331 sq.ft",
    }]
    calls = _patch_run_query(monkeypatch, rows)

    from api.tools.cypher_tools import match_pasted_listing
    paste = (
        "Reserve Price: 32 lakhs  EMD Date:24/05/2026  Auction: 25/05/2026\n"
        "Sai Nila Flats, Plot No.46, Balaraman Nagar, Poonamallee Chennai – 600056"
    )
    out = match_pasted_listing(paste)

    cypher, params = calls[0]
    assert "a.reserve_price_num >= $min_price" in cypher
    assert "a.reserve_price_num <= $max_price" in cypher
    assert "a.auction_start_dt >= $starts_after" in cypher
    assert "a.auction_start_dt <= $starts_before" in cypher
    # The whole point of v2 — no geographic constraint in the primary
    # Cypher. The OPTIONAL MATCHes for city/area are allowed (we still
    # populate city/area on the returned rows for display); what must be
    # absent is the filtering MATCH and any city/area parameter.
    assert "(a)-[:LOCATED_IN_CITY]->(c:City {name: $city})" not in cypher
    assert "(a)-[:LOCATED_IN_AREA]->(ar:Area)" not in cypher
    assert "city" not in params
    assert "area" not in params

    assert out["match"]["auction_id"] == "750879"


def test_sai_nila_canonical_fixture_high_confidence(monkeypatch) -> None:
    """The regression case. A description that contains all the discriminative
    tokens from the paste (built-up 741 sqft, UDS 331 sqft, "Balaraman
    Nagar", plot 46) must score ≥ 0.85 even though the row's `city` is
    Tiruvallur (NOT Chennai). This is the failure that motivated v2."""
    rows = [{
        "auction_id": "750879",
        "title": "Bajaj Housing Finance Ltd Flat Auction in Poonamallee taluk, Tiruvallur",
        "reserve_price": 3_200_000,
        "auction_start": "2026-05-25T11:00:00",
        "city": "Tiruvallur",
        "area": "Poonamallee taluk",
        "bank": "Bajaj Housing Finance Ltd",
        # Real graph description — 750879 actually has these tokens verbatim.
        "description": (
            "Schedule \"A\": All that piece and parcel of property being Flat "
            "No. S-1 in the Second floor of an Area of 741 sq.ft (including "
            "common area) along with a car parking together with 331 sq.ft., "
            "undivided share of land in the total extent measuring 1620 sq "
            "ft., comprised in Survey No.533 of Poonamalle village bearing "
            "plot No 46, Balaraman Nagar, Poonamallee, Chennai"
        ),
    }]
    _patch_run_query(monkeypatch, rows)

    from api.tools.cypher_tools import match_pasted_listing
    paste = (
        "🏦BANK E-AUCTION PROPERTY🏦\n👉POONAMALLEE\n"
        "Flat No S1, Second Floor, Sai Nila Flats\n"
        "Plot No.46, Balaraman Nagar, Poonamallee Chennai – 600056\n"
        "Built up area: 741 Sqft\nUDS: 331 sqft\n2BHK Flat\n"
        "Reserve Price: 32 lakhs\nEMD Date:24/05/2026\nAuction: 25/05/2026"
    )
    out = match_pasted_listing(paste)

    assert out["match"]["auction_id"] == "750879"
    assert out["confidence"] >= 0.85, (
        f"Sai Nila canonical case must score ≥ 0.85, got {out['confidence']}"
    )
    assert out["widening_reason"] is None


def test_score_only_price_and_date_match(monkeypatch) -> None:
    """A bare candidate (price+date match, but description has none of the
    paste's discriminative tokens) gets the floor confidence — high enough
    to surface (≥ 0.6) but not high enough to call it a definite match."""
    rows = [{
        "auction_id": "999999",
        "reserve_price": 3_200_000,
        "auction_start": "2026-05-25T11:00:00",
        "city": "Chennai", "area": "Adyar",
        "description": "Some other flat, no overlap with the paste at all",
    }]
    _patch_run_query(monkeypatch, rows)

    from api.tools.cypher_tools import match_pasted_listing
    paste = (
        "Sai Nila Flats Plot No.46 Balaraman Nagar Poonamallee – 600056\n"
        "Built up area 741 sqft UDS 331 sqft\n"
        "Reserve 32 lakhs Auction 25/05/2026"
    )
    out = match_pasted_listing(paste)
    # price (0.30) + date (0.30) = 0.60. None of the description tokens hit.
    assert 0.55 <= out["confidence"] < 0.75


def test_score_locality_token_in_description_boosts(monkeypatch) -> None:
    """When two candidates have identical price+date, the one whose
    description contains the paste's distinctive locality token wins."""
    rows = [
        {
            "auction_id": "AAA",
            "reserve_price": 3_200_000,
            "auction_start": "2026-05-25T11:00:00",
            "city": "Chennai", "area": "Adyar",
            "description": "Bare candidate, no special tokens",
        },
        {
            "auction_id": "BBB",
            "reserve_price": 3_200_000,
            "auction_start": "2026-05-25T11:00:00",
            "city": "Tiruvallur", "area": "Poonamallee taluk",
            "description": "Flat in Balaraman Nagar Poonamallee — 741 sq.ft",
        },
    ]
    _patch_run_query(monkeypatch, rows)

    from api.tools.cypher_tools import match_pasted_listing
    paste = (
        "Plot No.46, Balaraman Nagar, Poonamallee Chennai – 600056\n"
        "Built up area: 741 Sqft  Reserve 32 lakhs  Auction 25/05/2026"
    )
    out = match_pasted_listing(paste)
    assert out["match"]["auction_id"] == "BBB"
    # BBB matched: price + date + 741 + balaraman nagar → ≥ 0.85
    assert out["confidence"] >= 0.85


def test_extracted_fields_returned(monkeypatch) -> None:
    _patch_run_query(monkeypatch, [])

    from api.tools.cypher_tools import match_pasted_listing
    out = match_pasted_listing(
        "Reserve Price: 32 lakhs  Auction: 25/05/2026  Poonamallee Chennai – 600056\n"
        "Built up area: 741 Sqft  UDS: 331 sqft  Plot No.46  Balaraman Nagar"
    )
    extracted = out["extracted"]
    assert extracted["reserve_price"] == 3_200_000
    assert extracted["auction_date"] == "2026-05-25"
    assert extracted["pin"] == "600056"
    assert extracted["built_up_sqft"] == 741
    assert extracted["uds_sqft"] == 331
    assert extracted["plot_no"] == "46"
    assert "balaraman nagar" in extracted["locality_tokens"]


# ── Progressive widening tests ────────────────────────────────────────────

def _patch_tiered_run_query(monkeypatch, tiers: list[list[dict]]):
    calls: list[tuple[str, dict]] = []
    state = {"i": 0}

    def fake_run_query(cypher: str, params: dict | None = None):
        calls.append((cypher, dict(params or {})))
        i = state["i"]
        state["i"] += 1
        return list(tiers[i]) if i < len(tiers) else []

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_query", fake_run_query)
    monkeypatch.setattr(
        ct, "run_read_query",
        lambda cypher, params=None, timeout=10.0, max_rows=200: fake_run_query(cypher, params),
    )
    monkeypatch.setattr(ct, "_load_known_locations", lambda: (
        {"Chennai"}, {"Poonamallee", "Ambattur"},
    ))
    return calls


def test_widens_when_strict_returns_nothing(monkeypatch) -> None:
    """Strict price+date returns []. Widening drops the date constraint
    and finds a sibling property at the same price — surfaces with an
    explanatory `widening_reason`, never an empty candidates list."""
    sibling = {
        "auction_id": "999111",
        "reserve_price": 3_200_000,
        "auction_start": "2026-07-15T11:00:00",
        "city": "Chennai", "area": "Poonamallee",
        "description": "Another Poonamallee flat, different date",
    }
    calls = _patch_tiered_run_query(monkeypatch, [[], [sibling]])

    from api.tools.cypher_tools import match_pasted_listing
    out = match_pasted_listing(
        "Reserve Price: 32 lakhs  Auction: 25/05/2026  Poonamallee Chennai – 600056"
    )

    assert out["match"] is None
    assert out["confidence"] == 0.0
    assert out["candidates"], "must surface CLOSE candidates, never empty"
    assert out["candidates"][0]["auction_id"] == "999111"
    assert out["widening_reason"] is not None
    assert "date" in out["widening_reason"].lower()
    assert len(calls) == 2


def test_widens_through_multiple_tiers_until_hits(monkeypatch) -> None:
    """Strict empty, drop-date empty, widen-price+drop-date finds something."""
    cousin = {
        "auction_id": "888222",
        "reserve_price": 3_500_000,
        "auction_start": "2026-09-10T11:00:00",
        "city": "Chennai", "area": "Ambattur",
        "description": "Different date and looser price band",
    }
    calls = _patch_tiered_run_query(monkeypatch, [
        [],         # tier 0 strict (price ±2% + date ±2 days)
        [],         # tier 1 (drop date)
        [cousin],   # tier 2 (drop date + widen price ±10%)
    ])

    from api.tools.cypher_tools import match_pasted_listing
    out = match_pasted_listing(
        "Reserve Price: 32 lakhs  Auction: 25/05/2026  Poonamallee Chennai – 600056"
    )
    assert out["candidates"][0]["auction_id"] == "888222"
    assert out["widening_reason"] is not None
    assert "price" in out["widening_reason"].lower()
    assert len(calls) == 3


def test_widens_returns_empty_when_even_loosest_misses(monkeypatch) -> None:
    """All tiers exhausted → genuinely empty candidates, but extracted
    fields still surface so the LLM can ask the user for help."""
    _patch_tiered_run_query(monkeypatch, [[], [], [], []])

    from api.tools.cypher_tools import match_pasted_listing
    out = match_pasted_listing(
        "Reserve Price: 32 lakhs  Auction: 25/05/2026  Poonamallee Chennai – 600056"
    )
    assert out["match"] is None
    assert out["candidates"] == []
    assert out["extracted"]["reserve_price"] == 3_200_000


def test_locality_tokens_added_to_strict_cypher(monkeypatch) -> None:
    """When the paste yields locality tokens (e.g. 'balaraman nagar'), the
    strict Cypher must filter on them as `OR`-joined description CONTAINS
    clauses — otherwise a wide price-only filter (especially with no date)
    returns dozens of irrelevant candidates and scoring picks the wrong one."""
    rows = [{"auction_id": "750879", "reserve_price": 3_200_000,
             "auction_start": "2026-05-25T11:00:00",
             "city": "Tiruvallur", "area": "Poonamallee taluk",
             "description": "Sai Nila Flats Plot No 46 Balaraman Nagar 741 sq.ft"}]
    calls = _patch_run_query(monkeypatch, rows)

    from api.tools.cypher_tools import match_pasted_listing
    match_pasted_listing(
        "Sai Nila Flats Plot No.46 Balaraman Nagar Poonamallee Chennai 600056\n"
        "Reserve 32 lakhs"
    )
    cypher, params = calls[0]
    # Locality tokens become description-CONTAINS clauses, OR-joined,
    # case-insensitive.
    assert "toLower(a.description) CONTAINS" in cypher
    # Both extracted locality tokens are bound as params.
    assert any("balaraman nagar" in str(v).lower() for v in params.values())


def test_dateless_paste_still_finds_property_via_locality(monkeypatch) -> None:
    """The Sai Nila paste WITHOUT EMD/auction date still finds 750879. This
    is the regression case from the live test — without locality narrowing,
    a price-only filter returned ~30 candidates and scoring picked an
    unrelated Kanyakumari property."""
    rows = [{
        "auction_id": "750879",
        "reserve_price": 3_200_000,
        "auction_start": "2026-05-25T11:00:00",
        "city": "Tiruvallur", "area": "Poonamallee taluk",
        "description": (
            "Schedule A: Flat No. S-1, Second floor of an Area of 741 sq.ft "
            "with 331 sq.ft undivided share of land, plot No 46, Balaraman "
            "Nagar, Poonamallee village"
        ),
    }]
    _patch_run_query(monkeypatch, rows)

    from api.tools.cypher_tools import match_pasted_listing
    paste = (
        "🏦BANK E-AUCTION PROPERTY🏦\n👉POONAMALLEE\n"
        "Flat No S1, Second Floor, Sai Nila Flats\n"
        "Plot No.46, Balaraman Nagar, Poonamallee Chennai – 600056\n"
        "Built up area: 741 Sqft\nUDS: 331 sqft\n2BHK Flat\n"
        "Reserve Price: 32 lakhs"
    )
    out = match_pasted_listing(paste)

    assert out["match"]["auction_id"] == "750879"
    # No date in paste, so date weight (0.30) is dropped. With price (0.30) +
    # 741 (0.15) + 331 (0.10) + balaraman nagar (0.10) + plot 46 (0.05),
    # confidence should be ≥ 0.65.
    assert out["confidence"] >= 0.65


def test_widening_drops_locality_first(monkeypatch) -> None:
    """If the strict Cypher with locality+price+date returns nothing, the
    first widening tier must drop LOCALITY (often a typo / OCR error /
    abbreviation), not price or date. This keeps the high-signal price+date
    constraint in place while loosening the most volatile signal."""
    sibling = {
        "auction_id": "999333",
        "reserve_price": 3_200_000,
        "auction_start": "2026-05-25T11:00:00",
        "city": "Chennai", "area": "Poonamallee",
        "description": "Different building, same price and date",
    }
    # Tier 0 (price+date+locality) empty; tier 1 (drop locality) finds sibling.
    calls = _patch_tiered_run_query(monkeypatch, [[], [sibling]])

    from api.tools.cypher_tools import match_pasted_listing
    out = match_pasted_listing(
        "Sai Nila Flats Plot No.46 Balaraman Nagar Poonamallee – 600056\n"
        "Reserve Price: 32 lakhs  Auction: 25/05/2026"
    )
    assert out["candidates"][0]["auction_id"] == "999333"
    assert out["widening_reason"] is not None
    assert "locality" in out["widening_reason"].lower() or "description" in out["widening_reason"].lower()
    assert len(calls) == 2  # strict + drop-locality


def test_strict_cypher_uses_limit_50(monkeypatch) -> None:
    """v2's LIMIT 20 chopped real candidates when the price band was wide.
    v3 raises it to 50 so a price-only filter doesn't arbitrarily exclude
    rows beyond the cutoff."""
    calls = _patch_run_query(monkeypatch, [])

    from api.tools.cypher_tools import match_pasted_listing
    match_pasted_listing(
        "Reserve Price: 32 lakhs  Auction: 25/05/2026  Poonamallee – 600056"
    )
    cypher, _ = calls[0]
    assert "LIMIT 50" in cypher
    assert "LIMIT 20" not in cypher


def test_widening_reason_is_None_when_strict_match_succeeds(monkeypatch) -> None:
    rows = [{
        "auction_id": "750879", "reserve_price": 3_200_000,
        "auction_start": "2026-05-25T11:00:00",
        "city": "Tiruvallur", "area": "Poonamallee taluk",
        "description": "Sai Nila Flats Plot No 46 Balaraman Nagar 741 sq.ft 331 sq.ft",
    }]
    _patch_run_query(monkeypatch, rows)

    from api.tools.cypher_tools import match_pasted_listing
    out = match_pasted_listing(
        "Sai Nila Flats Plot No.46 Balaraman Nagar Poonamallee Chennai 600056\n"
        "Built up area: 741 Sqft  UDS: 331 sqft  Reserve 32 lakhs  Auction 25/05/2026"
    )
    assert out["match"]["auction_id"] == "750879"
    assert out["widening_reason"] is None
