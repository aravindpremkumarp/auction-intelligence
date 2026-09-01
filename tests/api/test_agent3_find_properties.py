"""Tests for api/agent3/find_properties.py — the agent3 search tool.

Pure: every Neo4j call is stubbed. What is asserted is the *shape* of the
Cypher (a lot filter must not become a join) and the contract the agent reads
(scope tags, refine, relax, errors as data).
"""
from __future__ import annotations

import pytest

from api.agent3 import find_properties as FP
from api.agent3.common import SQFT_CEIL, ToolSink


def _stub(monkeypatch, *, total=5, rows=None, extra=None):
    """Stub run_read_query. Returns the aggregate row first, then rows, then
    whatever refine/relax asks for."""
    calls: list[tuple[str, dict]] = []
    queue = list(extra or [])

    def fake(cypher, params=None, timeout=10.0, max_rows=200):
        calls.append((cypher, dict(params or {})))
        if "total_count" in cypher and "count(a) AS total_count" in cypher:
            return [{"total_count": total, "reserve_min": 100000.0,
                     "reserve_max": 900000.0, "reserve_avg": 500000.0,
                     "reserve_known": total,
                     "auction_first": "2026-01-05T11:00:00Z",
                     "auction_last": "2026-09-10T12:00:00Z",
                     "upcoming_count": 27}]
        if "auction_id AS auction_id" in cypher or "a.auction_id AS auction_id" in cypher:
            return list(rows or [])
        if queue:
            return queue.pop(0)
        return []

    monkeypatch.setattr(FP, "run_read_query", fake)
    return calls


def _row(auction_id="A1", lot_count=1, sqft_min=1000.0, sqft_max=1000.0,
         max_attempt=1, **kw):
    base = {"auction_id": auction_id, "title": "t", "city": "Chennai",
            "area": "Anna Nagar", "district": "Chennai", "bank": "SBI",
            "asset_category": "Residential", "auction_type": "SARFAESI Auction",
            "property_types": ["Flat"], "reserve_price": 4000000.0,
            "emd": 400000.0, "auction_start": None, "deadline": None,
            "url": "http://x", "lot_count": lot_count, "sqft_min": sqft_min,
            "sqft_max": sqft_max, "max_attempt": max_attempt}
    base.update(kw)
    return base


# ── the lot layer ────────────────────────────────────────────────────────

def test_lot_filters_are_exists_not_joins(monkeypatch):
    """A join through HAS_LOT multiplies `a` by its lot count, which would
    skew every aggregate computed in the same query. EXISTS cannot."""
    calls = _stub(monkeypatch, rows=[_row()])
    FP.find_properties(possession="physical", area_sqft_min=1000)

    cypher = calls[0][0]
    assert "EXISTS {" in cypher
    assert "MATCH (a)-[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->(l:Lot)-[:POSSESSION_IS]" in cypher
    # the lot path must appear only inside EXISTS, never as a top-level MATCH
    for line in cypher.splitlines():
        if line.strip().startswith("MATCH ") and "HAS_LOT" in line:
            pytest.fail(f"lot path joined at top level: {line}")


def test_sqft_filter_clamps_to_the_plausible_band(monkeypatch):
    """sqft_norm's max in the graph is 15,571,959,480 against a median of
    1,471. An unclamped ceiling lets one parse error into every average."""
    calls = _stub(monkeypatch, rows=[_row()])
    FP.find_properties(area_sqft_min=500, area_sqft_max=10 ** 12)

    params = calls[0][1]
    assert params["sqft_hi"] == SQFT_CEIL
    assert params["sqft_lo"] == 500.0


def test_sqft_filter_prefers_the_headline_extent(monkeypatch):
    calls = _stub(monkeypatch, rows=[_row()])
    FP.find_properties(area_sqft_min=1000)
    assert "_e.is_headline" in calls[0][0]


def test_inverted_sqft_band_is_an_error_not_an_empty_result(monkeypatch):
    _stub(monkeypatch)
    out = FP.find_properties(area_sqft_min=5000, area_sqft_max=1000)
    assert "error" in out and "above" in out["error"]


# ── scope honesty ────────────────────────────────────────────────────────

def test_single_lot_notice_yields_a_property_measurement(monkeypatch):
    _stub(monkeypatch, total=1, rows=[_row(lot_count=1, sqft_min=714.0, sqft_max=714.0)])
    out = FP.find_properties(city="Chennai")
    row = out["rows"][0]
    assert row["area_sqft"] == 714.0
    assert row["area_sqft_scope"] == "lot"
    assert "notice_area_sqft_range" not in row


def test_multi_lot_notice_never_yields_a_property_measurement(monkeypatch):
    """The real shape of listing 744314: a 2-lot notice spanning 3,359-7,040
    sqft. Emitting a single `area_sqft` here is the scope_honesty failure."""
    _stub(monkeypatch, total=1,
          rows=[_row(lot_count=2, sqft_min=3359.0, sqft_max=7040.0)])
    out = FP.find_properties(city="Coimbatore")
    row = out["rows"][0]
    assert "area_sqft" not in row
    assert row["notice_area_sqft_range"] == [3359.0, 7040.0]
    assert row["area_sqft_scope"] == "notice"
    assert row["notice_lot_count"] == 2


def test_notice_level_filters_are_declared_in_the_result(monkeypatch):
    _stub(monkeypatch, rows=[_row()])
    out = FP.find_properties(possession="physical")
    assert any("possession" in n for n in out["scope_notes"])


def test_listing_only_filters_produce_no_scope_notes(monkeypatch):
    _stub(monkeypatch, rows=[_row()])
    out = FP.find_properties(city="Chennai", reserve_price_max=5000000)
    assert "scope_notes" not in out


# ── the contract the agent reads ─────────────────────────────────────────

def test_broad_result_carries_refine_so_no_second_search_is_needed(monkeypatch):
    _stub(monkeypatch, total=412, rows=[_row()],
          extra=[[{"dimension": "area", "value": "Anna Nagar", "listings": 30}]])
    out = FP.find_properties(city="Chennai", limit=1)
    assert out["total_count"] == 412
    assert out["refine"] == [{"filter": "area", "value": "Anna Nagar", "listings": 30}]
    assert "do not re-search" in out["note"]


def test_zero_results_name_the_filter_to_drop(monkeypatch):
    _stub(monkeypatch, total=0,
          extra=[[{"dropped": "possession", "listings": 44},
                  {"dropped": "city", "listings": 3}]])
    out = FP.find_properties(city="Chennai", possession="physical")
    assert out["rows"] == []
    assert out["relax"][0]["drop_filter"] == "possession"
    assert out["relax"][0]["listings_if_dropped"] == 44
    assert "possession" in out["hint"]


def test_relax_hides_options_that_lead_to_another_zero(monkeypatch):
    _stub(monkeypatch, total=0,
          extra=[[{"dropped": "city", "listings": 0},
                  {"dropped": "possession", "listings": 7}]])
    out = FP.find_properties(city="Nowhere", possession="physical")
    assert [r["drop_filter"] for r in out["relax"]] == ["possession"]


def test_single_filter_zero_gives_a_hint_without_a_relax_list(monkeypatch):
    _stub(monkeypatch, total=0)
    out = FP.find_properties(city="Nowhere", upcoming_only=False)
    assert out["relax"] == []
    assert "do not loosen filters" in out["hint"]


def test_group_by_returns_buckets_and_skips_rows(monkeypatch):
    _stub(monkeypatch, total=100,
          extra=[[{"value": "Chennai", "listings": 60},
                  {"value": "Salem", "listings": 40}]])
    out = FP.find_properties(group_by="city")
    assert out["group_by"] == "city"
    assert out["distribution"][0] == {"value": "Chennai", "listings": 60}
    assert out["rows"] == []


# ── input handling ───────────────────────────────────────────────────────

def test_bad_enum_returns_the_valid_values_as_data(monkeypatch):
    """Raising kills the whole turn in the deepagents tool node. The model
    corrects itself far more reliably when shown the options."""
    _stub(monkeypatch)
    out = FP.find_properties(possession="vacant")
    assert "error" in out
    assert out["field"] == "possession"
    assert "physical" in out["valid_values"]


def test_bad_group_by_returns_the_dimensions(monkeypatch):
    _stub(monkeypatch)
    out = FP.find_properties(group_by="pincode")
    assert "valid_values" in out and "city" in out["valid_values"]


def test_asset_category_is_case_insensitive(monkeypatch):
    calls = _stub(monkeypatch, rows=[_row()])
    FP.find_properties(asset_category="residential")
    assert calls[0][1]["asset_category"] == "Residential"


def test_property_type_phrasing_is_expanded(monkeypatch):
    """'warehouse' still expands to the portal's Godown and Shed, but those
    names are then resolved to buckets — the filter runs on the
    notice-derived `property_type_effective`, not the portal edge, because
    832 listings live are filed under a portal type their notice contradicts.
    """
    calls = _stub(monkeypatch, rows=[_row()])
    FP.find_properties(property_type="warehouse")
    assert calls[0][1]["property_buckets"] == ["commercial", "industrial"]
    assert "a.property_type_effective IN $property_buckets" in calls[0][0]
    assert "HAS_PROPERTY_TYPE" not in calls[0][0]


def test_a_land_search_also_matches_plots(monkeypatch):
    """The two sources routinely pick different words for one patch of bare
    ground. Someone asking for land did not ask to exclude what a notice
    happened to call a plot."""
    calls = _stub(monkeypatch, rows=[_row()])
    FP.find_properties(property_type="plot")
    assert calls[0][1]["property_buckets"] == ["land", "plot"]


def test_upcoming_only_is_the_default(monkeypatch):
    """489 of 2,964 listings are still ahead of today. A buyer asking for
    'flats in Chennai' does not mean the 2,475 that already ran."""
    calls = _stub(monkeypatch, rows=[_row()])
    FP.find_properties(city="Chennai")
    assert "a.auction_start_dt >= $now" in calls[0][0]

    calls2 = _stub(monkeypatch, rows=[_row()])
    FP.find_properties(city="Chennai", upcoming_only=False)
    assert "$now" not in calls2[0][0]


def test_limit_is_capped(monkeypatch):
    _stub(monkeypatch, total=1000, rows=[_row(f"A{i}") for i in range(60)])
    out = FP.find_properties(limit=999)
    assert len(out["rows"]) <= 50


def test_bad_date_string_is_reported_not_silently_dropped(monkeypatch):
    _stub(monkeypatch)
    out = FP.find_properties(auction_from="next tuesday")
    assert "error" in out and "ISO" in out["error"]


# ── the sink ─────────────────────────────────────────────────────────────

def test_sink_takes_the_panel_rows_and_the_model_takes_a_slice(monkeypatch):
    """An unsplit payload becomes a ToolMessage that is checkpointed and
    re-sent on every later turn — the re-billing the deep-loop A/B measured."""
    calls = _stub(monkeypatch, total=200, rows=[_row(f"A{i}") for i in range(120)])
    sink = ToolSink()
    out = FP.find_properties(city="Chennai", limit=5, sink=sink)
    assert len(out["rows"]) == 5
    assert len(sink.panel_rows) == 120
    assert sink.auction_ids[:2] == ["A0", "A1"]
    # the row query must fetch for the PANEL, not for the model's slice —
    # fetching `limit` would leave the panel showing 5 of 200 matches
    row_call = next(c for c in calls if "a.auction_id AS auction_id" in c[0])
    assert row_call[1]["row_limit"] == FP.PANEL_ROW_CAP


def test_without_a_sink_only_the_model_slice_is_fetched(monkeypatch):
    calls = _stub(monkeypatch, total=200, rows=[_row()])
    FP.find_properties(city="Chennai", limit=5)
    row_call = next(c for c in calls if "a.auction_id AS auction_id" in c[0])
    assert row_call[1]["row_limit"] == 5


# ── identifiers ──────────────────────────────────────────────────────────

def test_identifier_filter_resolves_to_ids_then_filters_on_them(monkeypatch):
    calls = _stub(monkeypatch, rows=[_row()])
    monkeypatch.setattr(FP, "resolve_identifier", lambda v, k=None, limit=200: ["A1", "A2"])
    FP.find_properties(identifier="123/4B")
    assert "a.auction_id IN $identifier_ids" in calls[0][0]
    assert calls[0][1]["identifier_ids"] == ["A1", "A2"]


# The Lucene-escaping and dual-path (Lot/Parcel) resolution behaviour of
# resolve_identifier() itself is tested in test_agent3_identifiers.py, where
# it lives now — api.agent3.identifiers. This file only tests that
# find_properties composes correctly around whatever ids that seam returns.


# ── model payload trimming ───────────────────────────────────────────────

def test_url_is_stripped_from_the_model_rows(monkeypatch):
    """Measured at 295 tokens across 20 rows — 10% of the row payload, which
    is itself 92% of what a search sends the model. The model cites by
    auction_id and never needs a link, so this is pure cost."""
    _stub(monkeypatch, total=1, rows=[_row()])
    out = FP.find_properties(city="Chennai")
    assert "url" not in out["rows"][0]
    assert out["rows"][0]["auction_id"] == "A1"


def test_url_still_reaches_the_panel(monkeypatch):
    """The sink and the model are fed from the same shaped rows, so stripping
    in the shaper would silently take the link away from the matches panel
    too — and the panel is what turns a result into a clickable card."""
    _stub(monkeypatch, total=1, rows=[_row()])
    sink = ToolSink()
    FP.find_properties(city="Chennai", sink=sink)
    assert sink.panel_rows[0]["url"] == "http://x"


def test_default_row_sample_is_ten(monkeypatch):
    """Rows dominate per-turn cost. 10 halves that against the old 20 while
    keeping a usable sample; counts and aggregations stay exact regardless."""
    _stub(monkeypatch, total=200, rows=[_row(f"A{i}") for i in range(40)])
    out = FP.find_properties(city="Chennai")
    assert len(out["rows"]) == FP.DEFAULT_MODEL_ROWS == 10


def test_counts_stay_exact_when_the_sample_is_small(monkeypatch):
    """The sample must never be mistaken for the answer set — total_count and
    aggregations are computed over every match."""
    _stub(monkeypatch, total=412, rows=[_row(f"A{i}") for i in range(40)],
          extra=[[{"dimension": "area", "value": "Anna Nagar", "listings": 30}]])
    out = FP.find_properties(city="Chennai")
    assert out["total_count"] == 412
    assert len(out["rows"]) == 10
    assert out["aggregations"]["listings_with_reserve_price"] == 412


# ── date aggregates: the sample is not the set ───────────────────────────

def test_aggregations_carry_the_date_range_and_upcoming_count(monkeypatch):
    """Found on a live run, and it produced a confidently wrong answer.

    Asked "how many residential auctions in Coimbatore?", the agent replied
    that the latest auction date was May 2026 and all had concluded. The
    graph's true latest was 10 Sep 2026, with 27 still upcoming.

    The mechanism: `aggregations` carried price only. With no exact date to
    cite, the model read the date range off the ROW SAMPLE — and the default
    sort is `deadline` ASC, so that sample is the OLDEST ten of 208. It was
    not hallucinating; it was answering a range question from a deliberately
    truncated, deliberately oldest-first slice.

    Exact aggregates are the fix. A sterner warning in `note` would not have
    been, because the model had nothing else to answer from.
    """
    _stub(monkeypatch, total=208, rows=[_row()])
    out = FP.find_properties(city="Coimbatore")
    agg = out["aggregations"]
    assert agg["auction_start_last"] == "2026-09-10T12:00:00Z"
    assert agg["auction_start_first"] == "2026-01-05T11:00:00Z"
    assert agg["upcoming_count"] == 27


def test_date_aggregates_are_exact_over_every_match_not_the_sample(monkeypatch):
    """The aggregate row is computed by its own query over the full match
    set, so it cannot be contaminated by however few rows the model is
    shown. This is what makes it safe to answer a range question from."""
    calls = _stub(monkeypatch, total=208, rows=[_row()])
    FP.find_properties(city="Coimbatore", limit=1)
    agg_cypher = next(c for c, _ in calls if "count(a) AS total_count" in c)
    assert "max(a.auction_start_dt)" in agg_cypher
    assert "LIMIT" not in agg_cypher.upper().split("RETURN")[-1]
