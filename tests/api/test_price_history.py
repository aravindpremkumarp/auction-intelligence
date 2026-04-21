"""Tests for the price-history wiring added on top of search_auctions and
get_auction_detail — the `previous_reserve_price` side-channel on search
rows and the `price_history` timeline on detail."""
from __future__ import annotations


# ── search_auctions: previous_reserve_price on UI rows ───────────────────────

def _patch_run_query(monkeypatch, *, total_count: int, rows: list[dict]) -> list:
    calls: list[tuple[str, dict]] = []
    state = {"call": 0}

    def fake(cypher: str, params: dict | None = None):
        calls.append((cypher, dict(params or {})))
        state["call"] += 1
        if state["call"] == 1:
            return [{"total_count": total_count}]
        return rows

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_query", fake)
    return calls


def test_search_auctions_row_cypher_requests_previous_price(monkeypatch) -> None:
    calls = _patch_run_query(monkeypatch, total_count=1, rows=[
        {"auction_id": "x", "previous_reserve_price": 5500000},
    ])
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(limit=5)

    row_cypher, _ = calls[1]
    assert "SAME_PROPERTY_AS" in row_cypher
    assert "previous_reserve_price" in row_cypher
    assert "max(prev.reserve_price_num)" in row_cypher
    assert out["results"][0]["previous_reserve_price"] == 5500000


def test_search_auctions_row_passthrough_when_no_previous(monkeypatch) -> None:
    _patch_run_query(monkeypatch, total_count=1, rows=[
        {"auction_id": "y", "previous_reserve_price": None},
    ])
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(limit=5)
    assert out["results"][0]["previous_reserve_price"] is None


# ── get_auction_detail: price_history timeline ───────────────────────────────

def _patch_detail(monkeypatch, rows: list[dict]) -> None:
    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_query", lambda c, p=None: rows)


def test_get_auction_detail_builds_price_history_when_siblings_present(monkeypatch) -> None:
    _patch_detail(monkeypatch, [{
        "fields": {
            "auction_id": "current",
            "title": "Flat in Adyar",
            "url": "http://x",
            "reserve_price_num": 3200000,
            "auction_start_dt": "2026-05-07T10:00:00",
        },
        "relationships": {},
        "documents": [],
        "siblings": [
            {
                "auction_id": "prev1",
                "title": "Flat in Adyar",
                "url": None,
                "reserve_price_num": 5504000,
                "auction_start_dt": "2026-01-15T10:00:00",
                "match_reason": "borrower_location",
                "confidence": "medium",
            },
        ],
    }])
    from api.tools.cypher_tools import get_auction_detail

    out = get_auction_detail("current")
    assert out is not None
    history = out["price_history"]
    assert len(history) == 2
    # Sorted oldest → newest.
    assert history[0]["auction_id"] == "prev1"
    assert history[0]["is_current"] is False
    assert history[0]["reserve_price_num"] == 5504000
    assert history[1]["auction_id"] == "current"
    assert history[1]["is_current"] is True
    assert history[1]["reserve_price_num"] == 3200000


def test_get_auction_detail_empty_history_when_no_siblings(monkeypatch) -> None:
    _patch_detail(monkeypatch, [{
        "fields": {"auction_id": "lone", "reserve_price_num": 100000},
        "relationships": {},
        "documents": [],
        "siblings": [],
    }])
    from api.tools.cypher_tools import get_auction_detail

    out = get_auction_detail("lone")
    assert out["price_history"] == []


def test_get_auction_detail_cypher_includes_same_property_clause(monkeypatch) -> None:
    captured: list[str] = []
    import api.tools.cypher_tools as ct

    def fake(cypher: str, params: dict | None = None):
        captured.append(cypher)
        return [{
            "fields": {"auction_id": "x"},
            "relationships": {},
            "documents": [],
            "siblings": [],
        }]

    monkeypatch.setattr(ct, "run_query", fake)

    from api.tools.cypher_tools import get_auction_detail
    get_auction_detail("x")

    assert captured, "run_query was not called"
    assert "SAME_PROPERTY_AS" in captured[0]
    assert "sibling:AuctionProperty" in captured[0]
