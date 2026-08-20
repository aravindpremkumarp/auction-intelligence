"""
tests/api/test_chat_v2_loop.py
------------------------------
The tiered loop's control flow, exercised offline: the model calls are stubbed
with scripted structured responses and the tools with a fake registry, so
these run with no LLM and no Neo4j.

What is worth pinning here is the routing — which tier a question lands in,
whether the scope carries or resets, and that a follow-up round cannot run
away — because those are the decisions that change what the user sees.
"""
from __future__ import annotations

import asyncio

import pytest

from api.chat.v2 import loop as L
from api.chat.v2.executor import TurnBudget
from api.chat.v2.schemas import (
    CypherSpec,
    Plan,
    PlannedCall,
    Recommendation,
    Synthesis,
)


class _StubAgent:
    """Returns the next scripted structured response, recording what it saw."""

    def __init__(self, script):
        self.script = list(script)
        self.seen: list[str] = []

    async def ainvoke(self, state):
        self.seen.append(state["messages"][0][1])
        return {"messages": [], "structured_response": self.script.pop(0)}


@pytest.fixture
def stub_tools(monkeypatch):
    """A registry standing in for the real graph tools."""
    calls: list[dict] = []

    def search_auctions(**kwargs):
        calls.append({"tool": "search_auctions", "args": kwargs})
        return {"total_count": 20, "results": [{"auction_id": "837057"},
                                               {"auction_id": "831476"}]}

    def get_auction_detail(**kwargs):
        calls.append({"tool": "get_auction_detail", "args": kwargs})
        return {"results": [{"auction_id": "837057", "reserve_price": 3_500_000}]}

    def describe_schema(**kwargs):
        return {"labels": ["AuctionProperty"], "cypher_patterns": {"rules": ["x"]}}

    def run_cypher(**kwargs):
        calls.append({"tool": "run_cypher", "args": kwargs})
        return {"rows": [{"bank": "SBI", "cheapest": 1_200_000}]}

    registry = {"search_auctions": search_auctions,
                "get_auction_detail": get_auction_detail}
    monkeypatch.setattr("api.chat.v2.executor.ALL_TOOLS", registry)
    monkeypatch.setattr(L, "CYPHER_TOOLS",
                        {"describe_schema": describe_schema, "run_cypher": run_cypher})
    return calls


def _wire(monkeypatch, *agents):
    """build_tier_agent hands back the stubs in construction order: planner,
    synthesizer, then any tier-3 composer."""
    queue = list(agents)
    monkeypatch.setattr("api.chat.v2.agents.build_tier_agent",
                        lambda **kw: queue.pop(0))


def _run(**kw):
    return asyncio.run(L.run_turn("cheapest flats in Chennai", **kw))


# ── tier 1 ──────────────────────────────────────────────────────────────────

def test_plan_execute_synthesize_is_two_model_calls(monkeypatch, stub_tools):
    """The headline claim: a plain question costs one planning call and one
    synthesis call, not a chain of them."""
    planner = _StubAgent([Plan(calls=[PlannedCall(tool="search_auctions",
                                                  args={"city": "Chennai"})])])
    synth = _StubAgent([Synthesis(answer="Three worth a look.")])
    _wire(monkeypatch, planner, synth)

    out = _run(budget=TurnBudget())

    assert out.answer == "Three worth a look."
    assert out.model_calls == 2
    assert out.tier == 1
    assert [c.tool for c in out.executed] == ["search_auctions"]


def test_every_planned_call_runs(monkeypatch, stub_tools):
    """A multi-lookup question issues its calls together — that is the whole
    latency win over a ReAct loop, which can only issue them one at a time."""
    planner = _StubAgent([Plan(calls=[
        PlannedCall(tool="search_auctions", args={"city": "Chennai"}),
        PlannedCall(tool="search_auctions", args={"city": "Coimbatore"}),
        PlannedCall(tool="get_auction_detail", args={"auction_id": "1"}),
    ])])
    synth = _StubAgent([Synthesis(answer="Chennai is cheaper.")])
    _wire(monkeypatch, planner, synth)

    out = _run(budget=TurnBudget())
    assert len(out.executed) == 3


def test_direct_answer_skips_tools_and_synthesis(monkeypatch, stub_tools):
    planner = _StubAgent([Plan(direct_answer="I cover Tamil Nadu auctions.")])
    _wire(monkeypatch, planner, _StubAgent([]))

    out = _run(budget=TurnBudget())
    assert out.answer == "I cover Tamil Nadu auctions."
    assert out.executed == []
    assert out.model_calls == 1


# ── scope ───────────────────────────────────────────────────────────────────

def test_carried_scope_reaches_the_tool_call(monkeypatch, stub_tools):
    """The planner emits only the change; code supplies the carried city."""
    planner = _StubAgent([Plan(calls=[PlannedCall(tool="search_auctions",
                                                  args={"max_price": 4_000_000})])])
    _wire(monkeypatch, planner, _StubAgent([Synthesis(answer="ok")]))

    _run(scope={"city": "Chennai"}, budget=TurnBudget())

    assert stub_tools[0]["args"] == {"city": "Chennai", "max_price": 4_000_000}


def test_reset_drops_the_carried_scope(monkeypatch, stub_tools):
    """A topic switch must not silently answer about the previous city."""
    planner = _StubAgent([Plan(scope="reset",
                               calls=[PlannedCall(tool="search_auctions",
                                                  args={"group_by": "bank"})])])
    _wire(monkeypatch, planner, _StubAgent([Synthesis(answer="SBI leads.")]))

    _run(scope={"city": "Chennai"}, budget=TurnBudget())

    assert "city" not in stub_tools[0]["args"]


def test_scope_is_harvested_from_what_executed(monkeypatch, stub_tools):
    planner = _StubAgent([Plan(calls=[PlannedCall(tool="search_auctions",
                                                  args={"max_price": 4_000_000})])])
    _wire(monkeypatch, planner, _StubAgent([Synthesis(answer="ok")]))

    out = _run(scope={"city": "Chennai"}, budget=TurnBudget())

    assert out.filters == {"city": "Chennai", "max_price": 4_000_000}
    assert out.last_total_count == 20
    assert out.last_ids == ["837057", "831476"]


def test_client_scope_is_sanitized_before_use(monkeypatch, stub_tools):
    """The client echoes the scope back, so an off-contract key must never
    reach the tool kwargs."""
    planner = _StubAgent([Plan(calls=[PlannedCall(tool="search_auctions", args={})])])
    _wire(monkeypatch, planner, _StubAgent([Synthesis(answer="ok")]))

    _run(scope={"city": "Chennai", "limit": 999}, budget=TurnBudget())

    assert "limit" not in stub_tools[0]["args"]


# ── tier 2 ──────────────────────────────────────────────────────────────────

def test_need_more_runs_a_second_round(monkeypatch, stub_tools):
    planner = _StubAgent([
        Plan(calls=[PlannedCall(tool="search_auctions", args={"city": "Chennai"})]),
        Plan(calls=[PlannedCall(tool="get_auction_detail", args={"auction_id": "837057"})]),
    ])
    synth = _StubAgent([
        Synthesis(answer="", need_more=Plan(calls=[
            PlannedCall(tool="get_auction_detail", args={"auction_id": "837057"})])),
        Synthesis(answer="837057 is the cheapest at Rs 35L."),
    ])
    _wire(monkeypatch, planner, synth)

    out = _run(budget=TurnBudget())

    assert out.tier == 2
    assert out.answer == "837057 is the cheapest at Rs 35L."
    assert [c.tool for c in out.executed] == ["search_auctions", "get_auction_detail"]


def test_need_more_cannot_run_away(monkeypatch, stub_tools):
    """On the final round the synthesizer is told need_more is unavailable —
    and if it asks anyway, the loop answers rather than looping."""
    planner = _StubAgent([
        Plan(calls=[PlannedCall(tool="search_auctions", args={})]),
        Plan(calls=[PlannedCall(tool="search_auctions", args={})]),
    ])
    always_more = Synthesis(answer="partial", need_more=Plan(
        calls=[PlannedCall(tool="search_auctions", args={})]))
    synth = _StubAgent([always_more, always_more])
    _wire(monkeypatch, planner, synth)

    out = _run(budget=TurnBudget())

    assert out.answer == "partial"
    assert out.model_calls == 4  # 2 rounds x (plan + synth), no more


def test_final_round_note_is_sent(monkeypatch, stub_tools):
    planner = _StubAgent([Plan(calls=[PlannedCall(tool="search_auctions", args={})]),
                          Plan(calls=[PlannedCall(tool="search_auctions", args={})])])
    synth = _StubAgent([
        Synthesis(answer="", need_more=Plan(calls=[PlannedCall(tool="search_auctions", args={})])),
        Synthesis(answer="done"),
    ])
    _wire(monkeypatch, planner, synth)

    _run(budget=TurnBudget())

    assert "need_more` is not available" in synth.seen[1]


# ── tier 3 ──────────────────────────────────────────────────────────────────

def test_cypher_request_routes_to_tier_three(monkeypatch, stub_tools):
    planner = _StubAgent([Plan(cypher_request="cheapest property per bank")])
    synth = _StubAgent([Synthesis(answer="SBI has the cheapest at Rs 12L.")])
    composer = _StubAgent([CypherSpec(cypher="MATCH (a) RETURN a",
                                      description="cheapest per bank")])
    _wire(monkeypatch, planner, synth, composer)

    out = _run(budget=TurnBudget())

    assert out.tier == 3
    assert [c.tool for c in out.executed] == ["describe_schema", "run_cypher"]
    assert out.answer == "SBI has the cheapest at Rs 12L."


def test_cypher_failure_retries_once_with_the_error(monkeypatch):
    """The first failure is usually a guessed property name; handing back the
    real error fixes it far more often than re-composing blind."""
    attempts = []

    def run_cypher(cypher="", **kwargs):
        attempts.append(cypher)
        if len(attempts) == 1:
            raise ValueError("Unknown property name: a.total_area")
        return {"rows": [{"n": 1}]}

    monkeypatch.setattr(L, "CYPHER_TOOLS", {
        "describe_schema": lambda **kw: {"labels": []},
        "run_cypher": run_cypher,
    })
    planner = _StubAgent([Plan(cypher_request="sizes per city")])
    synth = _StubAgent([Synthesis(answer="done")])
    composer = _StubAgent([CypherSpec(cypher="BAD"), CypherSpec(cypher="GOOD")])
    _wire(monkeypatch, planner, synth, composer)

    _run(budget=TurnBudget())

    assert attempts == ["BAD", "GOOD"]
    assert "Unknown property name" in composer.seen[1]


def test_schema_brief_drops_the_rules_already_in_the_prompt():
    brief = L._schema_brief({"labels": ["A"], "cypher_patterns": {"rules": ["x"]}})
    assert "cypher_patterns" not in brief
    assert "labels" in brief


# ── budget + degradation ────────────────────────────────────────────────────

def test_model_budget_stops_the_turn_gracefully(monkeypatch, stub_tools):
    planner = _StubAgent([Plan(calls=[PlannedCall(tool="search_auctions", args={})])])
    _wire(monkeypatch, planner, _StubAgent([]))

    out = _run(budget=TurnBudget(max_model_calls=0))

    assert out.answer  # a sentence the user can act on, not an exception
    assert out.model_calls == 0


def test_tool_failure_does_not_kill_the_turn(monkeypatch):
    monkeypatch.setattr("api.chat.v2.executor.ALL_TOOLS", {
        "search_auctions": lambda **kw: (_ for _ in ()).throw(RuntimeError("neo4j down")),
    })
    planner = _StubAgent([Plan(calls=[PlannedCall(tool="search_auctions", args={})])])
    synth = _StubAgent([Synthesis(answer="I couldn't reach the database.")])
    _wire(monkeypatch, planner, synth)

    out = _run(budget=TurnBudget())

    assert out.answer == "I couldn't reach the database."
    assert out.executed[0].error.startswith("RuntimeError")


# ── streaming events ────────────────────────────────────────────────────────

def test_status_events_tick_per_completed_call(monkeypatch, stub_tools):
    """The execute phase must not be silent — one status per result landing."""
    events = []
    planner = _StubAgent([Plan(calls=[
        PlannedCall(tool="search_auctions", args={"city": "Chennai"}),
        PlannedCall(tool="search_auctions", args={"city": "Salem"}),
    ])])
    _wire(monkeypatch, planner, _StubAgent([Synthesis(answer="ok")]))

    _run(budget=TurnBudget(), on_event=lambda e, p: events.append((e, p)))

    labels = [p["label"] for e, p in events if e == "status"]
    assert "Planning…" in labels
    assert sum("search auctions · 20 match" == label for label in labels) == 2
    assert any(e == "plan" for e, _ in events)


def test_status_label_reports_a_failure_honestly(monkeypatch, stub_tools):
    from api.chat.v2.executor import ExecutedCall
    assert L._status_label(ExecutedCall(tool="search_auctions", args={},
                                        error="boom")) == "search auctions — no result"


# ── result serialization ────────────────────────────────────────────────────

def test_truncation_trims_rows_before_cutting_text():
    """A half-cut JSON blob is worse to reason over than fewer whole rows."""
    from api.chat.v2.executor import ExecutedCall
    big = ExecutedCall(tool="search_auctions", args={}, result={
        "total_count": 500,
        "results": [{"auction_id": str(i), "pad": "x" * 200} for i in range(200)],
    })
    text = L._truncate([big], budget=4000)
    assert "rows truncated for context" in text
    assert "total_count" in text


def test_recommendation_is_carried_through(monkeypatch, stub_tools):
    rec = Recommendation(summary="3 of 20 worth a look.", ranked_by="budget fit")
    planner = _StubAgent([Plan(calls=[PlannedCall(tool="search_auctions", args={})])])
    _wire(monkeypatch, planner, _StubAgent([Synthesis(answer="ok", recommendation=rec)]))

    out = _run(budget=TurnBudget())
    assert out.recommendation.ranked_by == "budget fit"


# ── regressions found by the first live conversation ────────────────────────

def test_planner_prose_never_ships_as_the_answer_on_a_followup(monkeypatch, stub_tools):
    """Observed live: after a need_more round the planner decided no further
    calls were needed, and its `direct_answer` — written for the loop, not the
    user — shipped verbatim ("No additional calls needed — the
    get_auction_detail results already contain price_history"). A follow-up
    round must fall through to a final synthesis instead."""
    planner = _StubAgent([
        Plan(calls=[PlannedCall(tool="search_auctions", args={})]),
        Plan(direct_answer="No additional calls needed — the results already "
                           "contain price_history."),
    ])
    synth = _StubAgent([
        Synthesis(answer="", need_more=Plan(
            calls=[PlannedCall(tool="get_auction_detail", args={"auction_id": "1"})])),
        Synthesis(answer="None of the 20 had a failed earlier auction."),
    ])
    _wire(monkeypatch, planner, synth)

    out = _run(budget=TurnBudget())

    assert out.answer == "None of the 20 had a failed earlier auction."
    assert "No additional calls needed" not in out.answer


def test_first_round_direct_answer_still_ships(monkeypatch, stub_tools):
    """The fix must not break the ordinary case — a greeting or a
    can't-answer on round one is genuinely the user-facing answer."""
    planner = _StubAgent([Plan(direct_answer="I cover Tamil Nadu auctions.")])
    _wire(monkeypatch, planner, _StubAgent([]))

    assert _run(budget=TurnBudget()).answer == "I cover Tamil Nadu auctions."


def test_scope_filters_count_as_grounded_numbers(monkeypatch, stub_tools):
    """Restating the threshold the user just asked for ("under Rs 40,00,000")
    is not a fabrication. Flagging it was two thirds of the gate's fire rate
    on the first live conversation."""
    planner = _StubAgent([Plan(calls=[PlannedCall(tool="search_auctions", args={})])])
    _wire(monkeypatch, planner, _StubAgent([
        Synthesis(answer="Of the residential flats under ₹40,00,000 in Chennai, "
                         "837057 is cheapest.")]))

    out = _run(scope={"city": "Chennai", "max_price": 4000000}, budget=TurnBudget())

    assert out.gate.ok, out.gate.reason


# ── planner narration (found by the golden eval) ────────────────────────────

@pytest.mark.parametrize("text", [
    "The user is asking about RBI guidelines for e-auction of secured assets "
    "— this is off-graph regulatory context. I'll use internet_search to find "
    "the relevant circulars.",
    "I will call search_auctions with city=Chennai.",
    "Let me query the graph for that.",
    "This is a schema question, so I should use describe_schema.",
])
def test_narration_is_recognised(text):
    """The exact shape that shipped to a user as their answer and scored 0.2
    from the eval judge."""
    assert L._reads_as_narration(text)


@pytest.mark.parametrize("text", [
    "I can't track or send alerts for this auction — tap the Save button on "
    "the property card instead.",
    "I don't have litigation data; that sits outside what this platform covers.",
    "I cover Tamil Nadu bank auctions. Ask me about a city or a price range.",
    "The platform has exactly 3 asset categories: Residential, Commercial and "
    "Industrials.",
    "",
])
def test_legitimate_answers_are_not_narration(text):
    """First person is normal in a refusal — "I can't do that" is the correct
    answer, not a planner talking to itself. A guard that trips on those would
    suppress the very answers the refusal cases require."""
    assert not L._reads_as_narration(text)


def test_narration_falls_through_to_synthesis(monkeypatch, stub_tools):
    """Rather than shipping monologue, the loop writes a real answer from
    whatever it has."""
    planner = _StubAgent([Plan(direct_answer="The user is asking about RBI "
                                             "rules. I'll use internet_search.")])
    synth = _StubAgent([Synthesis(answer="RBI's SARFAESI rules require ...")])
    _wire(monkeypatch, planner, synth)

    out = _run(budget=TurnBudget())

    assert out.answer == "RBI's SARFAESI rules require ..."
    assert "I'll use internet_search" not in out.answer


def test_a_genuine_direct_answer_still_ships(monkeypatch, stub_tools):
    planner = _StubAgent([Plan(direct_answer="I can't set up alerts — use the "
                                             "Save button on the property card.")])
    _wire(monkeypatch, planner, _StubAgent([]))

    out = _run(budget=TurnBudget())

    assert out.answer.startswith("I can't set up alerts")
    assert out.model_calls == 1   # no wasted synthesis call


# ── regressions found by code review ────────────────────────────────────────

def test_truncation_does_not_mutate_the_executed_calls():
    """`_truncate` shapes a PROMPT. It used to trim through the reference the
    payload held, making the cut permanent: the next turn carried 5 scope ids
    instead of 25, the answer gate flagged every id past row 5 as invented,
    and the browser got a 5-row artifact under an answer saying "22 matches"."""
    from api.chat.v2.executor import ExecutedCall

    call = ExecutedCall(tool="search_auctions", args={}, result={
        "total_count": 200,
        "results": [{"auction_id": str(i), "pad": "x" * 200} for i in range(200)],
    })
    text = L._truncate([call], budget=4000)

    assert "rows truncated for context" in text          # the prompt was trimmed
    assert len(call.result["results"]) == 200            # the record was not
    assert "_note" not in call.result


def test_repeated_truncation_is_stable():
    """Tier 2 truncates twice — once at the 8 KB follow-up budget, once at the
    24 KB synthesis budget. The smaller budget must not shrink what the larger
    one can still show."""
    from api.chat.v2.executor import ExecutedCall

    call = ExecutedCall(tool="search_auctions", args={}, result={
        "total_count": 200,
        "results": [{"auction_id": str(i), "pad": "x" * 200} for i in range(200)],
    })
    L._truncate([call], budget=L.FOLLOWUP_BUDGET)
    wide = L._truncate([call], budget=10_000_000)

    assert "rows truncated" not in wide
    assert wide.count("auction_id") == 200


def test_tier3_survives_an_exhausted_tool_budget(monkeypatch, stub_tools):
    """execute_plan returns [] once the per-turn cap is spent. Indexing [0]
    turned a budget cap into a 500 — after the model work was already paid
    for."""
    planner = _StubAgent([Plan(cypher_request="cheapest per bank")])
    synth = _StubAgent([Synthesis(answer="I couldn't run that query.")])
    _wire(monkeypatch, planner, synth)

    out = _run(budget=TurnBudget(max_tool_calls=0))

    assert out.answer == "I couldn't run that query."
    assert out.executed and "budget exhausted" in out.executed[0].error


def test_tier3_survives_the_budget_running_out_mid_way(monkeypatch):
    """Enough budget for the schema read, none left for the query itself."""
    monkeypatch.setattr(L, "CYPHER_TOOLS", {
        "describe_schema": lambda **kw: {"labels": ["AuctionProperty"]},
        "run_cypher": lambda **kw: {"rows": []},
    })
    planner = _StubAgent([Plan(cypher_request="cheapest per bank")])
    synth = _StubAgent([Synthesis(answer="Couldn't complete that.")])
    composer = _StubAgent([CypherSpec(cypher="MATCH (a) RETURN a")])
    _wire(monkeypatch, planner, synth, composer)

    out = _run(budget=TurnBudget(max_tool_calls=1))

    assert out.answer == "Couldn't complete that."
    assert any("budget exhausted" in (c.error or "") for c in out.executed)
