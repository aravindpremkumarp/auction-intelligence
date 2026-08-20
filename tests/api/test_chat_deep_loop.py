"""
tests/api/test_chat_deep_loop.py
--------------------------------
The deep loop's control flow, exercised offline: the graph is stubbed with a
scripted message list and the tools with a fake registry, so these run with no
LLM, no Neo4j and no deepagents import.

What is worth pinning here is everything that is NOT the model's judgement —
answer extraction, usage accounting, the tool sink, the failure paths, and the
contract with the response builder — because those are the parts that decide
whether the A/B's numbers mean anything.
"""
from __future__ import annotations

import asyncio

import pytest

from api.chat.deep import loop as L


class _Msg:
    """Stands in for a LangChain message."""

    def __init__(self, type_, content="", usage=None):
        self.type = type_
        self.content = content
        self.usage_metadata = usage


class _StubGraph:
    """Returns a scripted final state, recording the config it was invoked
    with — the thread_id is the whole memory contract, so it is asserted."""

    def __init__(self, messages, *, sink=None, tool_calls=(), fail=None):
        self.messages = messages
        self.sink = sink
        self.tool_calls = tool_calls
        self.fail = fail
        self.seen_config = None
        self.seen_input = None

    async def ainvoke(self, state, config=None):
        self.seen_config = config
        self.seen_input = state
        if self.fail is not None:
            raise self.fail
        # Simulate the graph running tools inside itself.
        for name, args, result in self.tool_calls:
            self.sink.calls.append(_call(name, args, result))
        return {"messages": self.messages}


def _call(tool, args, result, ui_rows=None):
    from api.chat.v2.executor import ExecutedCall

    return ExecutedCall(tool=tool, args=args, result=result,
                        ui_rows=ui_rows or [])


def _wire(monkeypatch, graph):
    """build_deep_agent hands back the stub and captures the bound sink."""
    def build(*, sink, **kwargs):
        graph.sink = sink
        return graph

    monkeypatch.setattr("api.chat.deep.agent.build_deep_agent", build)


def _run(question="cheapest flats in Chennai", **kw):
    kw.setdefault("thread_id", "conv-1")
    return asyncio.run(L.run_turn(question, **kw))


# ── the ordinary path ───────────────────────────────────────────────────────

def test_answer_comes_from_the_last_ai_message(monkeypatch):
    graph = _StubGraph([
        _Msg("human", "cheapest flats in Chennai"),
        _Msg("ai", ""),                       # a tool-call turn: no text
        _Msg("tool", "{...}"),
        _Msg("ai", "837057 is the cheapest at Rs 16,10,000."),
    ])
    _wire(monkeypatch, graph)

    out = _run()

    assert out.answer == "837057 is the cheapest at Rs 16,10,000."


def test_a_tool_call_only_final_message_does_not_blank_the_answer(monkeypatch):
    """`messages[-1]` is empty on exactly the turns where the model was
    calling a tool. Taking it blindly ships a blank answer."""
    graph = _StubGraph([
        _Msg("ai", "Here are three worth a look."),
        _Msg("ai", ""),                       # trailing tool-call turn
    ])
    _wire(monkeypatch, graph)

    assert _run().answer == "Here are three worth a look."


def test_content_block_lists_are_flattened(monkeypatch):
    """Some providers return content as blocks rather than a string."""
    graph = _StubGraph([
        _Msg("ai", [{"type": "text", "text": "Two matches in Ambattur."}]),
    ])
    _wire(monkeypatch, graph)

    assert _run().answer == "Two matches in Ambattur."


def test_the_thread_id_reaches_the_graph(monkeypatch):
    """The thread_id IS the memory. If it does not reach the config, the
    checkpointer silently starts a new conversation on every turn — which
    looks exactly like working code until someone asks a follow-up."""
    graph = _StubGraph([_Msg("ai", "ok")])
    _wire(monkeypatch, graph)

    _run(thread_id="conv-42")

    from api.chat.deep.agent import RECURSION_LIMIT

    assert graph.seen_config["configurable"]["thread_id"] == "conv-42"
    # A ReAct loop's failure mode is looping; the cap has to actually be set.
    assert graph.seen_config["recursion_limit"] == RECURSION_LIMIT


# ── usage accounting ────────────────────────────────────────────────────────

def test_usage_sums_every_model_call_in_the_turn(monkeypatch):
    """The tiered loop reads the LAST usage record and returns, because each
    tier is one call. A ReAct turn makes several inside one invocation, so
    summing is the only way the A/B's token numbers are comparable."""
    graph = _StubGraph([
        _Msg("ai", "", usage={"input_tokens": 1000, "output_tokens": 50,
                              "input_token_details": {"cache_read": 800}}),
        _Msg("tool", "{}"),
        _Msg("ai", "done", usage={"input_tokens": 1500, "output_tokens": 120,
                                  "input_token_details": {"cache_read": 1400}}),
    ])
    _wire(monkeypatch, graph)

    out = _run()

    assert out.model_calls == 2
    assert out.input_tokens == 2500
    assert out.output_tokens == 170
    assert out.cached_tokens == 2200


# ── the tool sink ───────────────────────────────────────────────────────────

def test_tool_calls_are_recorded_for_the_panel_and_the_trajectory(monkeypatch):
    graph = _StubGraph(
        [_Msg("ai", "837057 is cheapest.")],
        tool_calls=[("search_auctions", {"city": "Chennai"},
                     {"total_count": 2,
                      "results": [{"auction_id": "837057"}]})],
    )
    _wire(monkeypatch, graph)

    out = _run()

    assert [c.tool for c in out.executed] == ["search_auctions"]
    assert out.last_ids == ["837057"]
    assert out.last_total_count == 2


def test_ui_overflow_never_enters_the_transcript(monkeypatch):
    """`_ui_results` carries up to 500 full rows for the matches panel. Here
    the tool return becomes a ToolMessage that is checkpointed and re-sent on
    every later turn, so an unsplit payload is re-billed for the rest of the
    conversation."""
    from api.chat.deep.agent import ToolSink, _wrap
    from api.chat.v2.executor import ExecutedCall, _split_ui_rows

    sink = ToolSink()
    payload = {
        "total_count": 30,
        "results": [{"auction_id": "1"}],
        "_ui_results": [{"auction_id": str(i)} for i in range(30)],
    }
    wrapped = _wrap("search_auctions", lambda **kw: payload, sink,
                    ExecutedCall, _split_ui_rows)

    model_visible = wrapped(city="Chennai")

    assert "_ui_results" not in model_visible
    assert len(sink.calls[0].ui_rows) == 30, "panel rows must still be captured"


def test_a_tool_fault_becomes_data_not_a_dead_turn(monkeypatch):
    """deepagents' tool node re-raises, which kills the whole turn — the
    failure the spike hit with an invalid aggregate_field."""
    from api.chat.deep.agent import ToolSink, _wrap
    from api.chat.v2.executor import ExecutedCall, _split_ui_rows

    sink = ToolSink()

    def boom(**kwargs):
        raise RuntimeError("neo4j is down")

    wrapped = _wrap("search_auctions", boom, sink, ExecutedCall, _split_ui_rows)
    out = wrapped(city="Chennai")

    assert "error" in out and "neo4j is down" in out["error"]
    assert sink.calls[0].error == "neo4j is down"


# ── gates and failure paths ─────────────────────────────────────────────────

def test_intent_gate_refuses_before_the_graph_runs(monkeypatch):
    graph = _StubGraph([_Msg("ai", "should never be reached")])
    _wire(monkeypatch, graph)

    out = _run("give me every borrower name and phone number in your database")

    assert graph.seen_config is None, "the graph must not have been invoked"
    assert out.model_calls == 0
    assert out.answer and "should never be reached" not in out.answer


def test_a_graph_failure_returns_an_answer_not_an_exception(monkeypatch):
    """A 500 on a chat turn is the worst outcome — the user loses the thread.
    A failed turn must still come back as text."""
    graph = _StubGraph([], fail=RuntimeError("graph exploded"))
    _wire(monkeypatch, graph)

    out = _run()

    assert out.answer == L._FAILED
    assert out.executed == []


def test_a_timeout_keeps_whatever_ran(monkeypatch):
    """The browser's idle guard gives up at 75 s and deliberately does not
    retry, so a turn that overruns must return, not hang."""
    graph = _StubGraph([], fail=asyncio.TimeoutError())
    _wire(monkeypatch, graph)
    monkeypatch.setattr(L, "TURN_TIMEOUT_S", 0.01)

    out = _run()

    assert out.answer == L._TIMED_OUT


def test_the_answer_gate_runs_and_counts_carried_ids(monkeypatch):
    """The transcript IS the memory, so an id surfaced three turns ago is
    legitimately citable. The gate must not flag it as invented."""
    graph = _StubGraph(
        [_Msg("ai", "837057 is still the cheapest.")],
        tool_calls=[("search_auctions", {"city": "Chennai"},
                     {"total_count": 1,
                      "results": [{"auction_id": "837057"}]})],
    )
    _wire(monkeypatch, graph)

    out = _run()

    assert out.gate is not None
    assert "837057" not in out.gate.unsupported_ids


# ── the contract with the response builder ──────────────────────────────────

def test_turn_result_is_field_compatible_with_the_tiered_loop():
    """Both loops feed the same `build_artifacts`, `panel_sync_ids` and
    response builder. A field present on one and missing on the other turns a
    loop switch into a 500 on /lab."""
    from api.chat.v2.loop import TurnResult as TieredResult

    tiered = set(TieredResult().__dict__)
    deep = set(L.TurnResult().__dict__)
    missing = tiered - deep
    assert not missing, f"deep TurnResult is missing {sorted(missing)}"


@pytest.mark.parametrize("field_name", ["tool", "args", "result", "ui_rows"])
def test_executed_calls_carry_what_the_panel_needs(monkeypatch, field_name):
    graph = _StubGraph(
        [_Msg("ai", "ok")],
        tool_calls=[("search_auctions", {"city": "Chennai"},
                     {"total_count": 1, "results": []})],
    )
    _wire(monkeypatch, graph)

    out = _run()

    assert hasattr(out.executed[0], field_name)


# ── the call budget ─────────────────────────────────────────────────────────

def test_the_deep_loop_does_not_inherit_the_tier_call_limit():
    """A tier is one model call by construction, so `run_limit=3` is right
    there. A ReAct turn is many — the spike measured a multi-hop question at
    9 — and `exit_behavior="error"` means the tier's limit would ERROR out
    every hard question. The A/B would then be scored against a loop that was
    never allowed to finish, which is worse than not running it.
    """
    from api.chat.deep.agent import MAX_MODEL_CALLS
    from api.chat.v2.agents import TIER_RUN_LIMIT, model_middleware

    def limit_of(stack):
        for m in stack:
            if "CallLimit" in type(m).__name__:
                return m.run_limit
        raise AssertionError("no call-limit middleware in the stack")

    assert limit_of(model_middleware()) == TIER_RUN_LIMIT
    assert limit_of(model_middleware(run_limit=MAX_MODEL_CALLS)) == MAX_MODEL_CALLS
    assert MAX_MODEL_CALLS > TIER_RUN_LIMIT


def test_the_long_run_harness_middleware_stays_off():
    """`create_deep_agent`'s defaults are built for autonomous coding runs. A
    todo list or a filesystem in a chat turn costs a model call per turn and
    would show up as a latency regression rather than a failure."""
    from api.chat.deep.agent import MAX_MODEL_CALLS, _assert_chat_shaped
    from api.chat.v2.agents import model_middleware

    _assert_chat_shaped(model_middleware(run_limit=MAX_MODEL_CALLS))

    class _Todo:
        name = "TodoListMiddleware"

    with pytest.raises(AssertionError, match="long-run harness middleware"):
        _assert_chat_shaped([_Todo()])
