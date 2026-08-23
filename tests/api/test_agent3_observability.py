"""Tests for what a chat turn records about itself.

The regression these exist for is not a crash. `POST /chat/agent3` computed
token counts, returned them to the browser and logged them — on a logger with
no handler attached in any environment. Five real conversations went through
it on 22 Aug 2026 and their cost is unrecoverable: not in Render's logs, not
in Logfire, not in the graph. Latency survived only because FastAPI's own
request spans happen to cover it.

So the assertions here are about *reachability* as much as content: the lines
have to be emitted on the channel `api/telemetry.py` actually ships, and the
numbers on them have to be this turn's.
"""
from __future__ import annotations

import asyncio
import logging

from api.agent3 import loop as L
from api.agent3.common import ToolInputError, tool
from api.observability import record


class _Msg:
    """A message shaped like the LangChain ones, without the dependency."""

    def __init__(self, type_, content="", usage=None, tool_calls=None):
        self.type = type_
        self.content = content
        self.usage_metadata = usage
        self.tool_calls = tool_calls or []


def _ai(in_tok, out_tok, cached=0, tool_calls=None):
    return _Msg("ai", "", {"input_tokens": in_tok, "output_tokens": out_tok,
                           "total_tokens": in_tok + out_tok,
                           "input_token_details": {"cache_read": cached}},
                tool_calls=tool_calls)


def _lines(caplog, op):
    return [r for r in caplog.records if r.getMessage().startswith(op)]


# ── the channel ──────────────────────────────────────────────────────────

def test_the_app_loggers_feed_the_one_logger_telemetry_ships():
    """`api/telemetry.py` attaches its Logfire handler to `auction.obs` and
    `api`. A module logging under a name outside both is a line nobody will
    ever read — which is exactly what happened to the token counts."""
    logging.getLogger("api")  # what configure_telemetry attaches to
    for name in ("api.agent3.loop", "api.agent3.router", "api.agent3"):
        chain = []
        node = logging.getLogger(name)
        while node:
            chain.append(node.name)
            node = node.parent
        assert "api" in chain, f"{name} does not propagate to the api logger"


def test_record_writes_its_fields_as_record_attributes(caplog):
    """Logfire reads structured fields off the LogRecord. Formatting them
    into the message only would leave every query in the UI a regex."""
    with caplog.at_level(logging.INFO, logger="auction.obs"):
        record("agent3.turn.usage", in_tok=18422, cached_tok=16128, out_tok=311)

    line = _lines(caplog, "agent3.turn.usage")[0]
    assert "in_tok=18422" in line.getMessage()
    assert line.in_tok == 18422
    assert line.cached_tok == 16128
    assert line.op == "agent3.turn.usage"


def test_record_does_not_collide_with_reserved_logrecord_names(caplog):
    """`logging` raises on an `extra` key that shadows a built-in attribute,
    and `module` is an easy field name to reach for. Reporting must not be
    able to kill the thing it reports on."""
    with caplog.at_level(logging.INFO, logger="auction.obs"):
        record("agent3.turn", module="find_properties", msg="x", in_tok=5)

    line = _lines(caplog, "agent3.turn")[0]
    assert "module=find_properties" in line.getMessage()
    assert line.module_ == "find_properties"
    assert line.in_tok == 5


# ── tool calls ───────────────────────────────────────────────────────────

def test_every_tool_call_is_timed_with_the_size_of_what_it_returned(caplog):
    @tool
    def find_things(limit=2):
        return {"rows": [1, 2], "total_count": 57}

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        out = find_things()

    assert out == {"rows": [1, 2], "total_count": 57}
    message = _lines(caplog, "agent3.tool")[0].getMessage()
    assert "tool=find_things" in message
    assert "result=ok" in message
    assert "rows=2" in message and "total_count=57" in message
    assert "elapsed_ms=" in message


def test_a_rejected_argument_is_recorded_even_though_the_turn_succeeds(caplog):
    """Rule 1 of common.py: a bad argument comes back as data, so the turn
    carries on and looks fine. Without this line a tool the model got wrong
    on every call would leave no trace at all."""
    @tool
    def pick(kind):
        raise ToolInputError("bad kind", valid_values=("a", "b"), field="kind")

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        out = pick(kind="z")

    assert out["valid_values"] == ["a", "b"], "the model lost its self-correction"
    message = _lines(caplog, "agent3.tool")[0].getMessage()
    assert "result=input_error" in message and "field=kind" in message


def test_a_tool_that_returns_an_error_payload_is_not_recorded_as_ok(caplog):
    @tool
    def broken(x):
        raise ValueError("no such column")

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        assert broken(x=1) == {"error": "no such column"}

    message = _lines(caplog, "agent3.tool")[0].getMessage()
    assert "result=error" in message and "err=ValueError" in message


def test_a_real_bug_still_raises_and_is_logged_as_an_error(caplog):
    """`common.tool` catches argument mistakes, not defects. A KeyError is a
    bug and must keep propagating — with a line saying so."""
    @tool
    def buggy():
        raise KeyError("auction_id")

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        try:
            buggy()
        except KeyError:
            pass
        else:
            raise AssertionError("a real bug was swallowed")

    message = _lines(caplog, "agent3.tool")[0].getMessage()
    assert "status=error" in message and "tool=buggy" in message


# ── the turn ─────────────────────────────────────────────────────────────

class _Agent:
    def __init__(self, messages, extra=None):
        self._messages = messages
        self._extra = extra or {}

    async def ainvoke(self, payload, config=None):
        return {"messages": self._messages, **self._extra}


def test_a_turn_records_its_token_usage_and_call_counts(caplog):
    """The line this asserts is the one that did not exist. Every number on
    it was already computed and handed to the browser; none of it was kept."""
    agent = _Agent([
        _Msg("human", "q"),
        _ai(100, 10, tool_calls=[{"name": "find_properties"}]),
        _Msg("tool", "{}"),
        _ai(200, 20, cached=180),
    ])

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        out = asyncio.run(L.run_turn("q", thread_id="t1", model_name="flash",
                                     agent=agent))

    message = _lines(caplog, "agent3.turn ")[0].getMessage()
    assert "in_tok=300" in message
    assert "out_tok=30" in message
    assert "cached_tok=180" in message
    assert "model_calls=2" in message and "tool_calls=1" in message
    assert "model=flash" in message and "thread=t1" in message
    assert "elapsed_ms=" in message
    assert out.usage["input_tokens"] == 300, "the returned usage drifted"


def test_the_turn_line_counts_this_turn_only(caplog):
    """With a checkpointer `ainvoke` returns the whole conversation. Summing
    it re-bills history — the A/B reported 49,550 input tokens against an
    actual 29,877 that way, and it read convincingly like a real cost."""
    agent = _Agent([
        _Msg("human", "turn 1"), _ai(1000, 100),
        _Msg("human", "turn 2"), _ai(200, 20),
    ])

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        asyncio.run(L.run_turn("turn 2", thread_id="t2", agent=agent))

    message = _lines(caplog, "agent3.turn ")[0].getMessage()
    assert "in_tok=200" in message and "model_calls=1" in message


def test_each_model_call_is_recorded_with_its_own_tokens(caplog):
    """The turn total cannot tell one fat call from four thin ones, and those
    have opposite fixes. `cached_tok` per call is what separates them."""
    agent = _Agent([
        _Msg("human", "q"),
        _ai(100, 10, tool_calls=[{"name": "find_properties"},
                                 {"name": "get_property"}]),
        _Msg("tool", "{}"),
        _ai(200, 20, cached=180),
    ])

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        asyncio.run(L.run_turn("q", thread_id="t3", agent=agent))

    calls = [r.getMessage() for r in _lines(caplog, "agent3.model_call")]
    assert len(calls) == 2, calls
    assert "call=1" in calls[0] and "tools=find_properties,get_property" in calls[0]
    assert "call=2" in calls[1] and "cached_tok=180" in calls[1]


def test_a_turn_that_reports_no_usage_still_records_the_call_counts(caplog):
    """A provider that returns no `usage_metadata` must not take the latency
    and the call counts down with it — zero tokens is a fact worth seeing."""
    agent = _Agent([_Msg("human", "q"), _Msg("ai", "an answer")])

    with caplog.at_level(logging.INFO, logger="auction.obs"):
        out = asyncio.run(L.run_turn("q", thread_id="t4", agent=agent))

    message = _lines(caplog, "agent3.turn ")[0].getMessage()
    assert "model_calls=1" in message and "in_tok=0" in message
    assert out.usage == {}, "an absent usage report was invented"


# ── the wiring that ships it ─────────────────────────────────────────────

def _fake_logfire(monkeypatch, calls):
    """A stand-in for the logfire package, so this can be tested without it.

    `configure_telemetry` is import-guarded and no-ops when logfire is
    missing, which means a bug in the wiring would be invisible on any box
    that skipped the optional dependency. Installed through `monkeypatch` so
    the real package is back in `sys.modules` for the next test.
    """
    import sys
    import types

    module = types.ModuleType("logfire")
    integrations = types.ModuleType("logfire.integrations")
    logging_mod = types.ModuleType("logfire.integrations.logging")

    class _Handler(logging.Handler):
        def emit(self, record):  # the real one exports; this one is enough
            pass

    logging_mod.LogfireLoggingHandler = _Handler
    for name, mod in (("logfire", module), ("logfire.integrations", integrations),
                      ("logfire.integrations.logging", logging_mod)):
        monkeypatch.setitem(sys.modules, name, mod)

    for name in ("configure", "instrument_pydantic_ai", "instrument_httpx",
                 "instrument_fastapi"):
        setattr(module, name, lambda *a, _n=name, **k: calls.append(_n))
    return _Handler


def test_telemetry_attaches_its_handler_to_both_app_loggers(monkeypatch):
    """The bug: only `auction.obs` was wired, so every `api.*` INFO line —
    the agent3 endpoint's token counts among them — reached no handler at all
    and was dropped by `logging.lastResort` for being below WARNING."""
    import api.telemetry as T

    calls: list[str] = []
    handler_cls = _fake_logfire(monkeypatch, calls)
    monkeypatch.setattr(T, "_configured", False)
    monkeypatch.setenv("LOGFIRE_TOKEN", "test-token")

    names = ("auction.obs", "api")
    before = {n: (list(logging.getLogger(n).handlers), logging.getLogger(n).level)
              for n in names}
    try:
        assert T.configure_telemetry() is True
        for name in names:
            logger_ = logging.getLogger(name)
            assert any(isinstance(h, handler_cls) for h in logger_.handlers), \
                f"{name} would ship nothing"
            assert logger_.level == logging.INFO, \
                f"{name} inherits WARNING and drops the happy path"
        assert "instrument_httpx" in calls, "no per-model-call spans"
    finally:
        for name, (handlers, level) in before.items():
            logging.getLogger(name).handlers = handlers
            logging.getLogger(name).setLevel(level)
        T._configured = False
