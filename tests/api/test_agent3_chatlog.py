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
