"""`get_auction_detail` takes a LIST of auction_ids in one call.

Production telemetry (Logfire, 13 days) showed the tool firing 3.73× per turn
that used it, worst case 15 — and because every extra LLM round-trip re-sends
the whole accumulated context, the top 20% of turns burned 61% of all input
tokens. The docstring used to *ask* the model to batch; the signature now
guarantees it, so these tests pin the batch contract:

  * one graph query for N ids (not N queries),
  * results in the caller's id order,
  * ids the graph doesn't hold reported under `missing_ids` rather than
    silently absent (the model would otherwise retry them),
  * anything past the per-call cap reported under `dropped_ids`.
"""
from __future__ import annotations

import api.tools.cypher_tools as ct


def _row(auction_id: str) -> dict:
    return {
        "fields": {"auction_id": auction_id, "title": f"Plot {auction_id}"},
        "relationships": {},
        "documents": [],
        "siblings": [],
    }


def _patch(monkeypatch, rows: list[dict]) -> list[dict]:
    """Stub the graph and record every params dict it was called with."""
    calls: list[dict] = []

    def fake(cypher, params=None, timeout=10.0, max_rows=200):
        calls.append(params or {})
        wanted = set((params or {}).get("ids") or [])
        return [r for r in rows if r["fields"]["auction_id"] in wanted]

    monkeypatch.setattr(ct, "run_read_query", fake)
    return calls


def test_many_ids_cost_one_graph_query(monkeypatch) -> None:
    calls = _patch(monkeypatch, [_row("1"), _row("2"), _row("3")])
    out = ct.get_auction_details(["1", "2", "3"])
    assert len(calls) == 1, "batched ids must not fan out into per-id queries"
    assert calls[0]["ids"] == ["1", "2", "3"]
    assert out["returned"] == 3
    assert out["requested"] == 3


def test_results_follow_caller_id_order(monkeypatch) -> None:
    # The graph returns rows in its own order; the agent's ranking is the id
    # order it asked for, and the panel renders straight off `results`.
    _patch(monkeypatch, [_row("2"), _row("3"), _row("1")])
    out = ct.get_auction_details(["3", "1", "2"])
    assert [r["auction_id"] for r in out["results"]] == ["3", "1", "2"]


def test_unknown_ids_are_reported_not_dropped(monkeypatch) -> None:
    _patch(monkeypatch, [_row("1")])
    out = ct.get_auction_details(["1", "nope"])
    assert [r["auction_id"] for r in out["results"]] == ["1"]
    assert out["missing_ids"] == ["nope"]


def test_duplicate_ids_collapse(monkeypatch) -> None:
    calls = _patch(monkeypatch, [_row("1")])
    out = ct.get_auction_details(["1", "1", " 1 "])
    assert calls[0]["ids"] == ["1"]
    assert out["returned"] == 1


def test_over_cap_ids_are_dropped_loudly(monkeypatch) -> None:
    ids = [str(i) for i in range(ct._DETAIL_MAX_IDS + 3)]
    calls = _patch(monkeypatch, [_row(i) for i in ids])
    out = ct.get_auction_details(ids)
    assert len(calls[0]["ids"]) == ct._DETAIL_MAX_IDS
    assert out["dropped_ids"] == ids[ct._DETAIL_MAX_IDS:]
    assert out["requested"] == len(ids)
    assert "_note" in out, "a silent truncation would read as 'that's all there is'"


def test_empty_input_does_not_hit_the_graph(monkeypatch) -> None:
    calls = _patch(monkeypatch, [])
    out = ct.get_auction_details([])
    assert calls == []
    assert out == {"results": [], "returned": 0, "requested": 0}


def test_single_id_helper_still_returns_one_record(monkeypatch) -> None:
    # api/properties/router.py (the REST detail endpoint) calls this shape.
    _patch(monkeypatch, [_row("42")])
    out = ct.get_auction_detail("42")
    assert out is not None and out["auction_id"] == "42"
    assert ct.get_auction_detail("missing") is None


def test_agent_tool_accepts_str_or_list(monkeypatch) -> None:
    """The agent-facing wrapper normalizes both arities onto the batch call.

    conftest replaces `api.agent` in sys.modules with a stub (so importing
    api.main never builds a real OpenRouter client), so load the real module
    under an alias — same trick as test_deferred_capabilities.py.
    """
    import importlib.util
    import sys
    from pathlib import Path

    if "api_agent_real" in sys.modules:
        agent_mod = sys.modules["api_agent_real"]
    else:
        spec = importlib.util.spec_from_file_location(
            "api_agent_real",
            Path(__file__).resolve().parents[2] / "api" / "agent.py",
        )
        agent_mod = importlib.util.module_from_spec(spec)
        sys.modules["api_agent_real"] = agent_mod
        spec.loader.exec_module(agent_mod)

    seen: list[list[str]] = []
    monkeypatch.setattr(
        agent_mod.T, "get_auction_details",
        lambda ids: seen.append(list(ids)) or {"results": [], "returned": 0},
    )
    fn = agent_mod.get_auction_detail
    fn("7")
    fn(["7", "8"])
    assert seen == [["7"], ["7", "8"]]
