"""Tests for the two counted narrowing diagnostics search_auctions attaches:

- `refine`: on a result far bigger than the model's row sample, top buckets
  for a couple of dimensions the search doesn't already constrain, so the
  broad-result nudge can offer exact, live, non-empty narrowing filters.
- `relax`: on an over-constrained zero (>=2 substantive filters combining to
  nothing), a leave-one-out naming which single filter to drop and the count
  that unlocks — so a dead-end zero becomes "loosen this one constraint".

Both reuse search_auctions' own distribution / count paths; `run_read_query`
is stubbed and routed by cypher/params so no live graph is needed.
"""
from __future__ import annotations


# ── refine (broad result) ───────────────────────────────────────────────────

def _patch_broad(monkeypatch, *, total: int, dists: dict[str, list[dict]]):
    """total_count for the agg, rows for the fetch, and per-dimension buckets
    for distribution queries (routed by the dimension node label)."""
    calls: list[tuple[str, dict]] = []
    _LABEL = {
        "property_type": "PropertyType", "area": "Area",
        "asset_category": "AssetCategory", "bank": "Bank",
    }

    def fake(cypher: str, params=None, timeout=10.0, max_rows=200):
        calls.append((cypher, dict(params or {})))
        if "count(DISTINCT a) AS auction_count" in cypher:
            for dim, label in _LABEL.items():
                if f":{label})" in cypher:
                    return dists.get(dim, [])
            return []
        if "count(a) AS total_count" in cypher:
            return [{"total_count": total}]
        if "ORDER BY" in cypher:  # row fetch
            return [{"auction_id": str(i)} for i in range(min(total, 500))]
        return []

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_read_query", fake)
    return calls


def test_refine_attaches_two_unconstrained_dimensions(monkeypatch) -> None:
    _patch_broad(monkeypatch, total=91, dists={
        "property_type": [{"value": "Flat", "auction_count": 40},
                          {"value": "Plot", "auction_count": 30},
                          {"value": "House", "auction_count": 21}],
        "area": [{"value": "Anna Nagar", "auction_count": 25},
                 {"value": "Adyar", "auction_count": 18}],
        "asset_category": [{"value": "Residential", "auction_count": 91}],
    })
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(city="Chennai")

    assert out["total_count"] == 91
    # First two priority dims (property_type, area) that aren't already
    # filtered and have >=2 buckets; capped at _MAX_REFINE_DIMS.
    assert list(out["refine"]) == ["property_type", "area"]
    assert out["refine"]["property_type"] == [
        {"value": "Flat", "count": 40},
        {"value": "Plot", "count": 30},
        {"value": "House", "count": 21},
    ]
    assert out["refine"]["area"] == [
        {"value": "Anna Nagar", "count": 25},
        {"value": "Adyar", "count": 18},
    ]


def test_refine_skips_already_constrained_dimension(monkeypatch) -> None:
    _patch_broad(monkeypatch, total=91, dists={
        "property_type": [{"value": "Flat", "auction_count": 40},
                          {"value": "Plot", "auction_count": 30}],
        "area": [{"value": "Anna Nagar", "auction_count": 25},
                 {"value": "Adyar", "auction_count": 18}],
        "asset_category": [{"value": "Residential", "auction_count": 60},
                           {"value": "Commercial", "auction_count": 31}],
    })
    from api.tools.cypher_tools import search_auctions

    # property_type is already a filter → don't suggest narrowing by it.
    out = search_auctions(city="Chennai", property_type="Flat")
    assert "property_type" not in out["refine"]
    assert list(out["refine"]) == ["area", "asset_category"]


def test_no_refine_when_result_fits_the_sample(monkeypatch) -> None:
    _patch_broad(monkeypatch, total=20, dists={
        "property_type": [{"value": "Flat", "auction_count": 12},
                          {"value": "Plot", "auction_count": 8}],
    })
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(city="Chennai")
    assert out["total_count"] == 20
    assert "refine" not in out  # <= the LLM row cap → model already sees enough


def test_refine_drops_single_bucket_dimension(monkeypatch) -> None:
    _patch_broad(monkeypatch, total=91, dists={
        "property_type": [{"value": "Flat", "auction_count": 91}],   # only one → useless
        "area": [{"value": "Anna Nagar", "auction_count": 50},
                 {"value": "Adyar", "auction_count": 41}],
        "asset_category": [{"value": "Residential", "auction_count": 60},
                           {"value": "Commercial", "auction_count": 31}],
    })
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(city="Chennai")
    # A dimension with one bucket can't narrow anything, so it's skipped and
    # the next candidate fills its slot.
    assert "property_type" not in out["refine"]
    assert list(out["refine"]) == ["area", "asset_category"]


# ── relax (over-constrained zero) ────────────────────────────────────────────

def _patch_leave_one_out(monkeypatch, counts, *, past: int = 0):
    """`counts` maps a frozenset of present substantive filter param-names to a
    total_count. The floored top-level query and each leave-one-out probe route
    through here; the no-floor past count returns `past`."""
    calls: list[tuple[str, dict]] = []
    _SUBSTANTIVE = {
        "min_price", "max_price", "min_emd", "max_emd", "city", "area",
        "property_type", "asset_category", "bank", "borrower",
        "auction_type", "branch_name", "service_provider",
    }

    def fake(cypher: str, params=None, timeout=10.0, max_rows=200):
        p = params or {}
        calls.append((cypher, dict(p)))
        if "count(a) AS total_count" in cypher:
            present = frozenset(k for k in _SUBSTANTIVE if k in p)
            if "starts_after" not in p:      # the no-floor past-matches probe
                return [{"total_count": past}]
            return [{"total_count": counts.get(present, 0)}]
        return []

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_read_query", fake)
    return calls


def test_relax_names_the_filter_to_drop(monkeypatch) -> None:
    # All three filters → 0; dropping max_price → 6, dropping property_type → 2,
    # dropping city → 0 (city isn't the problem).
    _patch_leave_one_out(monkeypatch, {
        frozenset({"max_price", "city", "property_type"}): 0,
        frozenset({"city", "property_type"}): 6,          # max_price dropped
        frozenset({"max_price", "property_type"}): 0,     # city dropped
        frozenset({"max_price", "city"}): 2,              # property_type dropped
    })
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(city="Chennai", property_type="Flat", max_price=2_000_000)

    assert out["total_count"] == 0
    # Sorted by how much each unlocks; the 0-unlock drop (city) is omitted.
    assert out["relax"] == [
        {"filter": "max_price", "matches": 6},
        {"filter": "property_type", "matches": 2},
    ]
    assert "max_price" in out["hint"] and "relax" in out["hint"]


def test_relax_needs_two_substantive_filters(monkeypatch) -> None:
    calls = _patch_leave_one_out(monkeypatch, {frozenset({"city"}): 0}, past=0)
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(city="Nowhere")
    assert "relax" not in out
    # One filter → no leave-one-out probes: just the floored count + the
    # no-floor past-matches count.
    assert len(calls) == 2


def test_relax_absent_when_no_single_drop_helps(monkeypatch) -> None:
    # Two filters, but dropping either alone still yields 0 → a genuine
    # multi-filter conflict, no `relax` offered.
    _patch_leave_one_out(monkeypatch, {
        frozenset({"city", "bank"}): 0,
        frozenset({"bank"}): 0,
        frozenset({"city"}): 0,
    }, past=0)
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(city="Chennai", bank="SBI")
    assert "relax" not in out


def test_relax_and_past_matches_combine_in_the_hint(monkeypatch) -> None:
    _patch_leave_one_out(monkeypatch, {
        frozenset({"max_price", "city"}): 0,
        frozenset({"city"}): 8,          # drop max_price → 8 upcoming
        frozenset({"max_price"}): 0,     # drop city → 0
    }, past=15)  # and 15 exist in the past
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(city="Chennai", max_price=1_000_000)
    assert out["relax"] == [{"filter": "max_price", "matches": 8}]
    assert out["past_matches"] == 15
    assert "past auction" in out["hint"]  # the tail mentions the past matches


def test_date_filters_are_not_relax_candidates(monkeypatch) -> None:
    from datetime import datetime, timezone
    calls = _patch_leave_one_out(monkeypatch, {frozenset({"city"}): 0}, past=0)
    from api.tools.cypher_tools import search_auctions

    # city + a caller-set window: only `city` is substantive, so no leave-one-out
    # and (window was explicit) no past-matches probe either.
    out = search_auctions(
        city="Chennai",
        starts_after=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert "relax" not in out and "hint" not in out
    assert len(calls) == 1
