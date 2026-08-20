"""
tests/api/test_chat_v2_executor.py
----------------------------------
The executor is where the tiered loop's latency win lands, and where two
LangChain middleware that cannot reach our tool calls have their semantics
implemented instead. Both need pinning:

  * concurrency — a plan of N slow calls must cost the slowest, not the sum;
  * error-as-data — no single tool failure may kill a turn (the exact bug
    that killed the spike's variant A on one bad `aggregate_field`).

Fully offline: a fake registry, no LLM, no Neo4j.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from api.chat.v2 import executor as ex
from api.chat.v2.executor import TurnBudget, execute_plan


def _call(tool, **args):
    return {"tool": tool, "args": args}


@pytest.fixture
def registry():
    def ok(**kwargs):
        return {"total_count": 1, "results": [{"auction_id": "1"}], "echo": kwargs}

    def slow(seconds=0.2, **kwargs):
        time.sleep(seconds)
        return {"slept": seconds}

    def bad_arg(**kwargs):
        raise ValueError("aggregate_field must be one of ['emd_num', 'reserve_price_num']")

    def explodes(**kwargs):
        raise RuntimeError("driver went away")

    async def web(query=""):
        await asyncio.sleep(0.01)
        return {"sources": [], "query": query}

    return {"ok": ok, "slow": slow, "bad_arg": bad_arg, "explodes": explodes,
            "web": web}


async def _run(calls, registry, **kw):
    return await execute_plan(calls, budget=TurnBudget(), registry=registry, **kw)


def test_runs_calls_concurrently(registry):
    """Four 200 ms calls must finish in roughly 200 ms, not 800. This is the
    entire premise of the tiered loop."""
    calls = [_call("slow", seconds=0.2) for _ in range(4)]
    started = time.perf_counter()
    out = asyncio.run(_run(calls, registry))
    elapsed = time.perf_counter() - started

    assert len(out) == 4
    assert elapsed < 0.6, f"took {elapsed:.2f}s — calls did not run in parallel"


def test_preserves_plan_order(registry):
    """Results come back in plan order even though they complete out of order,
    so the synthesizer reads them in the order the planner intended."""
    calls = [_call("slow", seconds=0.15), _call("ok"), _call("slow", seconds=0.05)]
    out = asyncio.run(_run(calls, registry))
    assert [c.tool for c in out] == ["slow", "ok", "slow"]


def test_bad_argument_becomes_data_not_an_exception(registry):
    """One bad argument must not kill the turn. The message carries the valid
    values so the synthesizer can say what went wrong."""
    out = asyncio.run(_run([_call("bad_arg"), _call("ok")], registry))

    assert len(out) == 2
    assert "aggregate_field must be one of" in out[0].error
    assert out[1].error is None


def test_unexpected_exception_is_contained(registry):
    out = asyncio.run(_run([_call("explodes"), _call("ok")], registry))
    assert out[0].error.startswith("RuntimeError")
    assert out[1].result["total_count"] == 1


def test_unknown_tool_names_the_alternatives(registry):
    out = asyncio.run(_run([_call("serch_auctions")], registry))
    assert "unknown tool" in out[0].error
    assert "'ok'" in out[0].error


def test_timeout_is_reported_not_raised(registry, monkeypatch):
    monkeypatch.setattr(ex, "TOOL_TIMEOUT_S", 0.05)
    out = asyncio.run(_run([_call("slow", seconds=0.5)], registry))
    assert "timed out" in out[0].error


def test_async_tool_is_awaited_not_threaded(registry):
    out = asyncio.run(_run([_call("web", query="sarfaesi")], registry))
    assert out[0].result["query"] == "sarfaesi"
    assert out[0].error is None


def test_on_complete_fires_per_call_as_they_land(registry):
    """The SSE stream ticks a status line per result; if this fired once at the
    end, the execute phase would be silent again."""
    seen = []
    calls = [_call("slow", seconds=0.05), _call("ok")]
    asyncio.run(_run(calls, registry, on_complete=seen.append))
    assert {c.tool for c in seen} == {"slow", "ok"}


def test_budget_caps_a_runaway_plan(registry):
    budget = TurnBudget(max_tool_calls=3)
    calls = [_call("ok") for _ in range(10)]
    out = asyncio.run(execute_plan(calls, budget=budget, registry=registry))
    assert len(out) == 3
    assert budget.tool_calls == 3


def test_budget_spans_tiers(registry):
    """The cap is per turn, not per call to execute_plan — a tier-2 round
    cannot reset it."""
    budget = TurnBudget(max_tool_calls=4)
    asyncio.run(execute_plan([_call("ok")] * 3, budget=budget, registry=registry))
    second = asyncio.run(execute_plan([_call("ok")] * 3, budget=budget,
                                      registry=registry, tier=2))
    assert len(second) == 1


def test_model_call_budget():
    budget = TurnBudget(max_model_calls=2)
    assert budget.take_model_call() is True
    assert budget.take_model_call() is True
    assert budget.take_model_call() is False


def test_timing_is_recorded(registry):
    out = asyncio.run(_run([_call("slow", seconds=0.05)], registry))
    assert out[0].ms >= 40


def test_tier_is_tagged(registry):
    out = asyncio.run(execute_plan([_call("ok")], budget=TurnBudget(),
                                   registry=registry, tier=3))
    assert out[0].tier == 3


def test_empty_plan(registry):
    assert asyncio.run(_run([], registry)) == []
