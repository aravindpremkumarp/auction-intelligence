"""Tests for the `bank` scope filter, `order_by` ordering, and the
`_ui_results` side-channel on `search_auctions` — the fixes for feedback
items d32d18ce (Canara Bank scope dropped + invented max_price) and
137e1558 (panel should show all matches without bloating LLM context).
"""
from __future__ import annotations


def _patch_run_query(monkeypatch, *, total_count: int = 0, rows: list[dict] | None = None):
    """Stub run_query. First call is the count aggregate (return total_count);
    subsequent calls are the row fetch (return `rows`)."""
    rows = rows or []
    calls: list[tuple[str, dict]] = []
    state = {"call": 0}

    def fake_run_query(cypher: str, params: dict | None = None):
        calls.append((cypher, dict(params or {})))
        state["call"] += 1
        if state["call"] == 1:
            return [{"total_count": total_count}]
        return rows

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_query", fake_run_query)
    monkeypatch.setattr(
        ct, "run_read_query",
        lambda cypher, params=None, timeout=10.0, max_rows=200: fake_run_query(cypher, params),
    )
    return calls


def test_bank_filter_adds_conducted_by_edge(monkeypatch) -> None:
    calls = _patch_run_query(monkeypatch, total_count=0)
    from api.tools.cypher_tools import search_auctions

    search_auctions(bank="Canara Bank", limit=0)

    cypher, params = calls[0]
    assert "(a)-[:CONDUCTED_BY]->(b:Bank)" in cypher
    assert "b.name IN $bank" in cypher
    assert params["bank"] == ["Canara Bank"]


def test_multi_bank_and_multi_city(monkeypatch) -> None:
    """List inputs for bank + city must produce IN-list Cypher and list
    params — covers 'Canara Bank or Indian Bank in Chennai or Coimbatore'."""
    calls = _patch_run_query(monkeypatch, total_count=0)
    from api.tools.cypher_tools import search_auctions

    search_auctions(
        bank=["Canara Bank", "Indian Bank"],
        city=["Chennai", "Coimbatore"],
        asset_category=["Residential", "Commercial"],
        limit=0,
    )

    cypher, params = calls[0]
    assert "b.name IN $bank" in cypher
    assert "c.name IN $city" in cypher
    assert "ac.name IN $asset_category" in cypher
    assert params["bank"] == ["Canara Bank", "Indian Bank"]
    assert params["city"] == ["Chennai", "Coimbatore"]
    assert params["asset_category"] == ["Residential", "Commercial"]


def test_bank_filter_combines_with_property_type_and_city(monkeypatch) -> None:
    """The specific shape feedback d32d18ce would need: bank + property_type
    + city, with cheapest-first ordering and no invented max_price."""
    calls = _patch_run_query(monkeypatch, total_count=5, rows=[{"auction_id": str(i)} for i in range(5)])
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(
        bank="Canara Bank",
        property_type="Land",
        city="Chennai",
        order_by="price_asc",
        limit=5,
    )

    row_cypher, row_params = calls[1]
    assert "(a)-[:CONDUCTED_BY]->(b:Bank)" in row_cypher
    assert "b.name IN $bank" in row_cypher
    assert "(a)-[:HAS_PROPERTY_TYPE]->(pt:PropertyType)" in row_cypher
    assert "pt.name IN $property_type" in row_cypher
    assert "(a)-[:LOCATED_IN_CITY]->(c:City)" in row_cypher
    assert "c.name IN $city" in row_cypher
    assert "ORDER BY a.reserve_price_num ASC" in row_cypher
    assert row_params["bank"] == ["Canara Bank"]
    assert "min_price" not in row_params and "max_price" not in row_params
    assert out["limit"] == 5
    assert out["total_count"] == 5


def test_order_by_price_desc(monkeypatch) -> None:
    calls = _patch_run_query(monkeypatch, total_count=0)
    from api.tools.cypher_tools import search_auctions
    search_auctions(order_by="price_desc", limit=3)
    row_cypher, _ = calls[1]
    assert "ORDER BY a.reserve_price_num DESC" in row_cypher


def test_order_by_default_is_deadline_asc(monkeypatch) -> None:
    calls = _patch_run_query(monkeypatch, total_count=0)
    from api.tools.cypher_tools import search_auctions
    search_auctions(limit=3)
    row_cypher, _ = calls[1]
    assert "ORDER BY a.auction_start_dt ASC" in row_cypher


def test_order_by_rejects_unknown_value(monkeypatch) -> None:
    _patch_run_query(monkeypatch)
    from api.tools.cypher_tools import search_auctions
    import pytest
    with pytest.raises(ValueError, match="order_by"):
        search_auctions(order_by="random", limit=1)


def test_ui_results_side_channel_when_matches_exceed_limit(monkeypatch) -> None:
    """When total_count > limit, the tool returns both a model-visible slice
    (<= limit) and a `_ui_results` overflow (up to the UI cap)."""
    all_rows = [{"auction_id": str(i)} for i in range(91)]
    calls = _patch_run_query(monkeypatch, total_count=91, rows=all_rows)
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(city="Chennai", property_type="Land", limit=20)

    assert out["total_count"] == 91
    assert len(out["results"]) == 20
    assert "_ui_results" in out
    assert len(out["_ui_results"]) == 91

    # The row query used the larger UI limit, not the model-visible 20.
    _, row_params = calls[1]
    assert row_params["limit"] >= 91


def test_llm_row_cap_bounds_model_visible_rows(monkeypatch) -> None:
    """A large model-requested `limit` is bounded for the LLM at
    `_LLM_ROWS_HARD_CAP`, so one search can't dump hundreds of rows into the
    prompt. The UI still receives the full set via `_ui_results`."""
    all_rows = [{"auction_id": str(i)} for i in range(300)]
    _patch_run_query(monkeypatch, total_count=300, rows=all_rows)
    from api.tools.cypher_tools import search_auctions, _LLM_ROWS_HARD_CAP

    out = search_auctions(city="Chennai", limit=200)

    assert out["total_count"] == 300
    # Model sees the cap, not the 200 it asked for.
    assert len(out["results"]) == _LLM_ROWS_HARD_CAP
    # UI side-channel still carries every match.
    assert len(out["_ui_results"]) == 300


def test_no_ui_results_key_when_matches_fit_in_limit(monkeypatch) -> None:
    all_rows = [{"auction_id": str(i)} for i in range(5)]
    _patch_run_query(monkeypatch, total_count=5, rows=all_rows)
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(city="Chennai", limit=20)

    assert out["total_count"] == 5
    assert len(out["results"]) == 5
    # No overflow — `_ui_results` key is omitted so the LLM sees a clean shape.
    assert "_ui_results" not in out


def test_search_auctions_includes_reauction_count_in_cypher(monkeypatch) -> None:
    """The row query joins SAME_PROPERTY_AS and returns a reauction_count."""
    calls = _patch_run_query(monkeypatch, total_count=1, rows=[{"auction_id": "a"}])
    from api.tools.cypher_tools import search_auctions

    search_auctions(city="Chennai", limit=5)

    row_cypher, _ = calls[1]
    assert "SAME_PROPERTY_AS" in row_cypher
    assert "reauction_count" in row_cypher


def test_search_auctions_derives_is_reauction_flag(monkeypatch) -> None:
    """The tool post-processes rows so each one has `is_reauction` derived
    from `reauction_count`, and rows missing the field still get False."""
    rows = [
        {"auction_id": "A", "reauction_count": 2},
        {"auction_id": "B", "reauction_count": 0},
        {"auction_id": "C"},  # simulate older row shape without the field
    ]
    _patch_run_query(monkeypatch, total_count=3, rows=rows)
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(city="Chennai", limit=10)
    by_id = {r["auction_id"]: r for r in out["results"]}
    assert by_id["A"]["is_reauction"] is True
    assert by_id["A"]["reauction_count"] == 2
    assert by_id["B"]["is_reauction"] is False
    assert by_id["B"]["reauction_count"] == 0
    assert by_id["C"]["is_reauction"] is False
    assert by_id["C"]["reauction_count"] == 0
