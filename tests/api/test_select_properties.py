"""Tests for `get_auctions_by_ids` (the `select_properties` agent tool) —
keeps the UI matches panel in sync when the agent presents a subset of
previously-found properties ("top three of those") without running a new
search, which used to leave the panel showing the stale result set.
"""
from __future__ import annotations


def _patch_run_query(monkeypatch, rows: list[dict]):
    """Stub run_read_query to return `rows` and record every call."""
    calls: list[tuple[str, dict]] = []

    def fake_run_query(cypher: str, params: dict | None = None,
                       timeout: float = 10.0, max_rows: int = 200):
        calls.append((cypher, dict(params or {})))
        return rows

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_read_query", fake_run_query)
    return calls


def _row(auction_id: str, **extra) -> dict:
    return {"auction_id": auction_id, "title": f"Property {auction_id}",
            "reauction_count": 0, **extra}


def test_preserves_caller_order(monkeypatch) -> None:
    """Rows come back in the agent's ranking order, not the DB's."""
    _patch_run_query(monkeypatch, [_row("B"), _row("C"), _row("A")])
    from api.tools.cypher_tools import get_auctions_by_ids

    out = get_auctions_by_ids(["A", "B", "C"])

    assert [r["auction_id"] for r in out["results"]] == ["A", "B", "C"]
    assert out["total_count"] == 3
    assert out["returned"] == 3
    assert "missing_ids" not in out


def test_missing_ids_reported(monkeypatch) -> None:
    _patch_run_query(monkeypatch, [_row("A")])
    from api.tools.cypher_tools import get_auctions_by_ids

    out = get_auctions_by_ids(["A", "GHOST"])

    assert [r["auction_id"] for r in out["results"]] == ["A"]
    assert out["missing_ids"] == ["GHOST"]
    assert out["total_count"] == 1


def test_empty_ids_short_circuits(monkeypatch) -> None:
    """No ids → no query at all."""
    calls = _patch_run_query(monkeypatch, [])
    from api.tools.cypher_tools import get_auctions_by_ids

    out = get_auctions_by_ids([])

    assert out == {"total_count": 0, "returned": 0, "results": []}
    assert calls == []


def test_blank_and_duplicate_ids_dropped(monkeypatch) -> None:
    calls = _patch_run_query(monkeypatch, [_row("A")])
    from api.tools.cypher_tools import get_auctions_by_ids

    out = get_auctions_by_ids(["A", "  ", "A", ""])

    assert calls[0][1]["ids"] == ["A"]
    assert out["returned"] == 1


def test_id_cap(monkeypatch) -> None:
    calls = _patch_run_query(monkeypatch, [])
    from api.tools.cypher_tools import _BY_IDS_MAX, get_auctions_by_ids

    get_auctions_by_ids([str(i) for i in range(_BY_IDS_MAX + 10)])

    assert len(calls[0][1]["ids"]) == _BY_IDS_MAX


def test_reauction_flags_derived(monkeypatch) -> None:
    """is_reauction mirrors reauction_count, same as search_auctions rows."""
    _patch_run_query(monkeypatch, [
        _row("A", reauction_count=2),
        _row("B", reauction_count=None),
    ])
    from api.tools.cypher_tools import get_auctions_by_ids

    out = get_auctions_by_ids(["A", "B"])

    a, b = out["results"]
    assert a["is_reauction"] is True and a["reauction_count"] == 2
    assert b["is_reauction"] is False and b["reauction_count"] == 0
