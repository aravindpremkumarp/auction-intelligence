"""
tests/api/test_eval_agent_bindings.py
-------------------------------------
The EVAL_AGENT selector and the v2 bindings' output shapes.

A migration gate is only meaningful if both agents are measured by the same
ruler, so what matters here is that `golden_v2` / `conversation_v2` produce
exactly the shapes the existing evaluators already score — including the
panel state, derived with the real helpers rather than a re-implementation.
Panel desync is precisely what the conversation evaluators exist to catch,
and an eval that models the panel differently from production cannot catch
it.

Offline: the loop is stubbed, so no LLM and no Neo4j.
"""
from __future__ import annotations

import asyncio

import pytest

from evals import tasks


# ── selection ───────────────────────────────────────────────────────────────

def test_defaults_to_v1(monkeypatch):
    monkeypatch.delenv("EVAL_AGENT", raising=False)
    assert tasks.agent_id() == "v1"
    assert tasks.golden_task() is tasks.golden_v1
    assert tasks.conversation_task() is tasks.conversation_v1


def test_selects_v2(monkeypatch):
    monkeypatch.setenv("EVAL_AGENT", "v2")
    assert tasks.golden_task() is tasks.golden_v2
    assert tasks.conversation_task() is tasks.conversation_v2


@pytest.mark.parametrize("value", ["", "V3", "tiered", "  "])
def test_unknown_value_falls_back_rather_than_crashing_a_ci_run(monkeypatch, value):
    monkeypatch.setenv("EVAL_AGENT", value)
    assert tasks.agent_id() == "v1"


def test_case_and_whitespace_tolerant(monkeypatch):
    monkeypatch.setenv("EVAL_AGENT", "  V2 ")
    assert tasks.agent_id() == "v2"


# ── v2 golden binding ───────────────────────────────────────────────────────

class _Call:
    def __init__(self, tool, args, result):
        self.tool, self.args, self.result = tool, args, result


class _Result:
    def __init__(self, answer, executed, filters=None, total=None, ids=None):
        self.answer = answer
        self.executed = executed
        self.filters = filters or {}
        self.last_total_count = total
        self.last_ids = ids or []


_ROWS = {"total_count": 2, "results": [{"auction_id": "837057"},
                                       {"auction_id": "831476"}]}


def test_golden_v2_shape(monkeypatch):
    async def fake_run_turn(question, **kwargs):
        return _Result("837057 is cheapest.",
                       [_Call("search_auctions", {"city": "Chennai"}, _ROWS)])

    monkeypatch.setattr("api.chat.v2.loop.run_turn", fake_run_turn)

    out = asyncio.run(tasks.golden_v2("cheapest in Chennai"))

    assert out.answer == "837057 is cheapest."
    assert out.tools_called == ["search_auctions"]
    assert out.surfaced_auction_ids == ["831476", "837057"]   # sorted, like v1
    assert out.cited_auction_ids == ["837057"]                # first-mention order


def test_golden_v2_dedupes_tools_in_call_order(monkeypatch):
    """ToolTrajectory scores an ordered, de-duplicated list on v1; v2 must
    produce the same shape or the two runs aren't comparable."""
    async def fake_run_turn(question, **kwargs):
        return _Result("ok", [
            _Call("search_auctions", {}, _ROWS),
            _Call("search_auctions", {}, _ROWS),
            _Call("get_auction_detail", {}, {}),
        ])

    monkeypatch.setattr("api.chat.v2.loop.run_turn", fake_run_turn)
    out = asyncio.run(tasks.golden_v2("q"))
    assert out.tools_called == ["search_auctions", "get_auction_detail"]


def test_golden_v2_never_cites_an_id_the_tools_did_not_return(monkeypatch):
    """The citation set is restricted to what was surfaced — the same
    protection v1 gets, and the reason CitesAuctionIds means anything."""
    async def fake_run_turn(question, **kwargs):
        return _Result("Try 999999.", [_Call("search_auctions", {}, _ROWS)])

    monkeypatch.setattr("api.chat.v2.loop.run_turn", fake_run_turn)
    assert asyncio.run(tasks.golden_v2("q")).cited_auction_ids == []


def test_golden_v2_handles_a_toolless_turn(monkeypatch):
    async def fake_run_turn(question, **kwargs):
        return _Result("I cover Tamil Nadu auctions.", [])

    monkeypatch.setattr("api.chat.v2.loop.run_turn", fake_run_turn)
    out = asyncio.run(tasks.golden_v2("what are you?"))
    assert out.tools_called == []
    assert out.surfaced_auction_ids == []


# ── v2 conversation binding ─────────────────────────────────────────────────

class _Convo:
    def __init__(self, messages):
        self.turns = [type("T", (), {"message": m})() for m in messages]


def test_conversation_v2_threads_the_scope_not_a_transcript(monkeypatch):
    """Substituting the scope object for the message history IS the v2 design;
    this is the binding that exercises it over a real narrowing conversation
    rather than a single question."""
    seen: list[dict] = []

    async def fake_run_turn(question, *, scope=None, last_ids=None,
                            last_total_count=None, **kwargs):
        seen.append({"q": question, "scope": dict(scope or {}),
                     "ids": list(last_ids or [])})
        merged = {**(scope or {}), "city": "Chennai"}
        if "40" in question:
            merged["max_price"] = 4000000
        return _Result(f"answer to {question}",
                       [_Call("search_auctions", merged, _ROWS)],
                       filters=merged, total=20, ids=["837057", "831476"])

    monkeypatch.setattr("api.chat.v2.loop.run_turn", fake_run_turn)

    out = asyncio.run(tasks.conversation_v2(
        _Convo(["flats in Chennai", "under 40 lakhs"])))

    assert seen[0]["scope"] == {}
    assert seen[1]["scope"] == {"city": "Chennai"}       # carried, not replayed
    assert seen[1]["ids"] == ["837057", "831476"]        # anchor ids available
    assert len(out.turns) == 2
    assert out.turns[1].active_filters == {"city": "Chennai", "max_price": 4000000}
    assert out.turns[1].total_count == 20


def test_conversation_v2_derives_the_panel_with_the_real_sync(monkeypatch):
    """PanelState and PanelReferenceResolution are two of the six gating
    evaluators, and the spike had no panel concept at all — this is the gap
    that binding closes."""
    async def fake_run_turn(question, **kwargs):
        return _Result("837057 and 831476 both fit.",
                       [_Call("search_auctions", {}, _ROWS)],
                       filters={}, total=2, ids=["837057", "831476"])

    monkeypatch.setattr("api.chat.v2.loop.run_turn", fake_run_turn)

    out = asyncio.run(tasks.conversation_v2(_Convo(["flats in Chennai"])))

    assert out.turns[0].panel_ids_before == []
    assert out.turns[0].panel_ids == ["837057", "831476"]


def test_conversation_v2_records_tool_args_for_the_forbid_assertions(monkeypatch):
    """`forbid_tool_arg_values` — how the topic-switch conversations catch a
    stale carried filter — reads this turn's search args."""
    async def fake_run_turn(question, **kwargs):
        return _Result("SBI leads.",
                       [_Call("search_auctions", {"group_by": "bank"}, _ROWS)])

    monkeypatch.setattr("api.chat.v2.loop.run_turn", fake_run_turn)
    out = asyncio.run(tasks.conversation_v2(_Convo(["which bank has the most?"])))

    assert out.turns[0].tool_calls == [
        {"tool": "search_auctions", "args": {"group_by": "bank"}}]


# ── the coverage gap this closes ────────────────────────────────────────────

def test_dimension_change_conversation_exists():
    """The middleware design named topic-switch RESET as the missing scenario.
    switch_replace_scope covers a pivot that REPLACES the city; this covers the
    harder one, where nothing in the new question supplies a replacement."""
    from evals.conversations import GOLDEN_CONVERSATIONS

    convo = next(c for c in GOLDEN_CONVERSATIONS if c.conv_id == "switch_drop_scope")
    pivot = convo.turns[1]
    assert pivot.topic_switch
    assert pivot.forbid_tool_arg_values == {"city": "Chennai", "max_price": 5000000}
