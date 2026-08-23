"""Tests for the transcript a turn leaves behind.

The gap these close: agent3 recorded what a turn *cost* — tokens, calls,
latency — and nothing about what it *said*. Reading a suspect answer meant
decoding base64 checkpoint blobs out of Neo4j, so in practice nobody read
one. These assert the text reaches the channel `api/telemetry.py` ships to
Logfire, in attributes a query can select, with the user's words intact and
the loop's own scaffolding left out.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from api.agent3 import chatlog as C
from api.agent3 import loop as L
from api.agent3.common import tool as C_tool


class _Msg:
    def __init__(self, type_, content="", tool_calls=None, name=None,
                 tool_call_id=None):
        self.type = type_
        self.content = content
        self.tool_calls = tool_calls or []
        self.name = name
        self.tool_call_id = tool_call_id


class _Agent:
    def __init__(self, messages):
        self._messages = messages

    async def ainvoke(self, payload, config=None):
        return {"messages": self._messages}


def _chatlog(caplog):
    lines = [r for r in caplog.records
             if r.getMessage().startswith("agent3.chatlog")]
    assert lines, "no chatlog line was emitted"
    return lines[0]


# ── the line ─────────────────────────────────────────────────────────────

def test_a_turn_records_the_question_and_the_answer(caplog):
    agent = _Agent([_Msg("human", "x"), _Msg("ai", "It sold for £182,000.")])

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        asyncio.run(L.run_turn("what did 748779 sell for?", thread_id="t1",
                               model_name="flash", agent=agent))

    line = _chatlog(caplog)
    assert line.question == "what did 748779 sell for?"
    assert line.answer == "It sold for £182,000."
    assert line.thread_ == "t1" and line.model == "flash"


def test_the_transcript_holds_the_users_words_not_the_composed_input(caplog):
    """`compose_input` prepends the date line and any skill text. Logging that
    would bury the question under scaffolding the user never wrote."""
    agent = _Agent([_Msg("human", "x"), _Msg("ai", "a")])

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        asyncio.run(L.run_turn("how many lots in Leeds?", thread_id="t1",
                               agent=agent))

    question = _chatlog(caplog).question
    assert question == "how many lots in Leeds?"
    assert "Today is" not in question


def test_tool_steps_pair_each_result_with_the_args_that_asked_for_it(caplog):
    agent = _Agent([
        _Msg("human", "x"),
        _Msg("ai", "", tool_calls=[{"id": "c1", "name": "find_properties",
                                    "args": {"town": "Leeds"}}]),
        _Msg("tool", '{"rows": 3}', name="find_properties", tool_call_id="c1"),
        _Msg("ai", "Three lots."),
    ])

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        asyncio.run(L.run_turn("q", thread_id="t1", agent=agent))

    line = _chatlog(caplog)
    steps = json.loads(line.steps_json)
    assert steps == [{"tool": "find_properties",
                      "args": '{"town": "Leeds"}',
                      "result": '{"rows": 3}'}]
    assert "tools=find_properties" in line.getMessage()


def test_a_result_whose_request_is_missing_still_appears():
    """An unrecoverable arg list is not a reason to drop the step — a gap in
    the transcript reads as "the model never called it"."""
    rows = C.steps([_Msg("tool", "{}", name="get_property", tool_call_id="z")],
                   limit=100)
    assert rows == [{"tool": "get_property", "args": "", "result": "{}"}]


def test_content_blocks_are_flattened_into_readable_text(caplog):
    """Some providers answer in blocks. Logging the repr of a list would make
    every such answer unreadable in the exact place someone goes to read it."""
    agent = _Agent([_Msg("human", "x"),
                    _Msg("ai", [{"type": "text", "text": "Sold "},
                                {"type": "text", "text": "in May."}])])

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        asyncio.run(L.run_turn("q", thread_id="t1", agent=agent))

    assert _chatlog(caplog).answer == "Sold in May."


# ── limits and the off switch ────────────────────────────────────────────

def test_long_text_is_clipped_and_says_how_much_it_dropped():
    out = C.clip("x" * 50, 10)
    assert out.startswith("x" * 10)
    assert "+40 chars" in out, "a silent truncation reads as a complete answer"


def test_the_cap_is_tunable_and_applies_to_the_answer(monkeypatch, caplog):
    monkeypatch.setenv("AGENT3_CHATLOG_MAX_CHARS", "200")
    agent = _Agent([_Msg("human", "x"), _Msg("ai", "y" * 500)])

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        asyncio.run(L.run_turn("q", thread_id="t1", agent=agent))

    answer = _chatlog(caplog).answer
    assert answer.startswith("y" * 200) and "+300 chars" in answer


@pytest.mark.parametrize("value", ["0", "false", "off", "NO"])
def test_an_operator_can_switch_transcript_capture_off(monkeypatch, caplog,
                                                       value):
    """This is user text leaving the box. There has to be one env var that
    stops it without a deploy."""
    monkeypatch.setenv("AGENT3_CHATLOG", value)
    agent = _Agent([_Msg("human", "x"), _Msg("ai", "a")])

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        asyncio.run(L.run_turn("q", thread_id="t1", agent=agent))

    assert not [r for r in caplog.records
                if r.getMessage().startswith("agent3.chatlog")]
    assert [r for r in caplog.records
            if r.getMessage().startswith("agent3.turn ")], \
        "the numbers must keep flowing when the text is switched off"


def test_a_broken_transcript_never_costs_the_answer(monkeypatch, caplog):
    """Observability that can fail a turn is worse than no observability."""
    monkeypatch.setattr(C, "steps",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    agent = _Agent([_Msg("human", "x"), _Msg("ai", "the answer")])

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        out = asyncio.run(L.run_turn("q", thread_id="t1", agent=agent))

    assert out.answer == "the answer"


# ── the failing turn ─────────────────────────────────────────────────────

class _Boom:
    """An agent whose turn dies the way a real one does — mid-graph."""

    def __init__(self, exc):
        self._exc = exc

    async def ainvoke(self, payload, config=None):
        raise self._exc


def test_a_turn_that_raises_still_leaves_a_transcript(caplog):
    """The turns worth reading were the only ones leaving nothing to read:
    `ainvoke` throws, so there are no messages and no answer to build one
    from."""
    with caplog.at_level(logging.INFO, logger="auction.obs"):
        try:
            asyncio.run(L.run_turn("why did 748779 not sell?", thread_id="t9",
                                   model_name="flash",
                                   agent=_Boom(RuntimeError("model timed out"))))
        except RuntimeError:
            pass
        else:
            raise AssertionError("the error was swallowed")

    line = _chatlog(caplog)
    assert line.question == "why did 748779 not sell?"
    assert line.outcome == "error"
    assert "RuntimeError: model timed out" in line.err
    assert line.thread_ == "t9"


def test_a_cancelled_turn_is_not_reported_as_an_error(caplog):
    """The streaming endpoint cancels the turn when the browser goes away.
    Counting those as errors invents an error rate out of closed tabs."""
    async def _cancelled():
        try:
            await L.run_turn("q", thread_id="t9", agent=_Boom(asyncio.CancelledError()))
        except asyncio.CancelledError:
            pass

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        asyncio.run(_cancelled())

    assert _chatlog(caplog).outcome == "cancelled"


def test_the_failing_turn_reports_the_tools_that_already_ran(caplog):
    """How far it got is the whole value of the line. The graph returns
    nothing on an exception, so this can only come from the tools
    themselves."""
    @C_tool
    def find_properties(town=None):
        return {"rows": [1, 2, 3], "total_count": 9}

    class _RunsThenDies:
        async def ainvoke(self, payload, config=None):
            find_properties(town="Leeds")
            raise RuntimeError("died after the tool")

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        try:
            asyncio.run(L.run_turn("q", thread_id="t9", agent=_RunsThenDies()))
        except RuntimeError:
            pass

    step = json.loads(_chatlog(caplog).steps_json)[0]
    assert step["tool"] == "find_properties"
    assert step["args"] == {"town": "Leeds"}
    assert step["result"] == "ok" and step["rows"] == 3
    assert isinstance(step["ms"], int)


def test_a_successful_turn_is_marked_ok(caplog):
    agent = _Agent([_Msg("human", "x"), _Msg("ai", "a")])

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        asyncio.run(L.run_turn("q", thread_id="t1", agent=agent))

    line = _chatlog(caplog)
    assert line.outcome == "ok"
    assert not hasattr(line, "err")


# ── the tool line ────────────────────────────────────────────────────────

def test_a_tool_call_carries_the_thread_it_ran_for(caplog):
    """`agent3.tool` could only be tied to a conversation through the trace,
    and a trace is exactly what a log drain does not have."""
    @C_tool
    def get_property(auction_id=None):
        return {"ok": True}

    class _CallsTool:
        async def ainvoke(self, payload, config=None):
            get_property(auction_id="748779")
            return {"messages": [_Msg("human", "x"), _Msg("ai", "done")]}

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        asyncio.run(L.run_turn("q", thread_id="t-42", agent=_CallsTool()))

    line = [r for r in caplog.records
            if r.getMessage().startswith("agent3.tool")][0]
    assert "thread=t-42" in line.getMessage()
    assert line.thread_ == "t-42"


def test_a_tool_called_outside_a_turn_still_works(caplog):
    """Eval scripts and tests call tools directly. No context is not an
    error — it just means there is no thread to name."""
    @C_tool
    def standalone():
        return {"ok": True}

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        assert standalone() == {"ok": True}

    message = [r for r in caplog.records
               if r.getMessage().startswith("agent3.tool")][0].getMessage()
    assert "thread=" not in message


def test_the_turn_context_does_not_leak_into_the_next_turn():
    """A leaked context labels the next turn's tool calls with the previous
    thread — worse than no label at all."""
    from api.agent3.common import current_turn, turn_context

    assert current_turn() is None
    with turn_context("t1") as ctx:
        assert current_turn() is ctx
    assert current_turn() is None


def test_the_thread_survives_the_hop_into_langchains_executor(caplog):
    """LangChain runs a sync tool off the event loop. It copies the context
    to get there — this pins that, because if it ever stopped, the thread id
    would silently vanish from every tool line again."""
    from langchain_core.runnables.config import run_in_executor

    from api.agent3.common import turn_context

    @C_tool
    def in_a_thread():
        return {"ok": True}

    async def _drive():
        with turn_context("t-executor"):
            await run_in_executor(None, in_a_thread)

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        asyncio.run(_drive())

    line = [r for r in caplog.records
            if r.getMessage().startswith("agent3.tool")][0]
    assert line.thread_ == "t-executor"
