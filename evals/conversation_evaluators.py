"""
evals/conversation_evaluators.py
--------------------------------
Conversation-level evaluators + the per-turn output types for the multi-turn
eval. Every evaluator reads the whole `ConversationOutput` (all turns) and the
matching per-turn expectations off `ctx.metadata["turns"]`, so it can assert
across turns (narrowing, carry-over) rather than one answer at a time.

All four are deterministic, pass/fail gates:

- ``ConversationTrajectory`` — every turn that declares `expected_tools` called
  at least one of them.
- ``MonotonicNarrowing`` — `total_count` never grows across the turns flagged
  `narrows` (the 50 → 3 funnel).
- ``FilterCarryOver`` — after each turn, the rolling scope (as re-derived by the
  router) contains the turn's `expect_filters` (value-exact, or present-with-
  any-value for the ``ANY`` sentinel).
- ``NoStaleScope`` — on a topic-switch turn, the search args do NOT carry a
  value the pivot dropped (`forbid_tool_arg_values`) — the stale-filter bug.

Kept separate from `evals/evaluators.py` (the single-turn evaluators) because
they read a fundamentally different output shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from evals.conversations import ANY

CONVERSATION_TRAJECTORY = "ConversationTrajectory"
MONOTONIC_NARROWING = "MonotonicNarrowing"
FILTER_CARRY_OVER = "FilterCarryOver"
NO_STALE_SCOPE = "NoStaleScope"

# The gating assertions (all must pass for a conversation to count as passed).
CONVERSATION_GATES = (
    CONVERSATION_TRAJECTORY,
    MONOTONIC_NARROWING,
    FILTER_CARRY_OVER,
    NO_STALE_SCOPE,
)


@dataclass
class TurnOutput:
    """What the runner captures for one turn of a conversation."""

    message: str
    answer: str
    tools_called: list[str] = field(default_factory=list)
    # Every tool call this turn, in order: [{"tool": str, "args": dict}, ...].
    tool_calls: list[dict] = field(default_factory=list)
    # This turn's search `total_count` (most recent search in the thread), or
    # None if the turn ran no search.
    total_count: int | None = None
    # The rolling scope AFTER this turn, re-derived exactly like the router.
    active_filters: dict = field(default_factory=dict)


@dataclass
class ConversationOutput:
    """The eval task's return for one scripted conversation."""

    turns: list[TurnOutput] = field(default_factory=list)


def _turns_out(ctx: EvaluatorContext) -> list:
    return getattr(ctx.output, "turns", None) or []


def _turns_spec(ctx: EvaluatorContext) -> list:
    return (ctx.metadata or {}).get("turns") or []


def _values_match(expected, actual) -> bool:
    """Loose filter-value equality: case-insensitive for strings, membership
    when the agent passed a list, exact otherwise."""
    if isinstance(actual, list):
        return any(_values_match(expected, a) for a in actual)
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip().lower() == actual.strip().lower()
    return expected == actual


@dataclass
class ConversationTrajectory(Evaluator):
    """Every turn with `expected_tools` must invoke at least one of them."""

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        outs, specs = _turns_out(ctx), _turns_spec(ctx)
        for spec, out in zip(specs, outs):
            acceptable = spec.get("expected_tools") or []
            if not acceptable:
                continue
            called = getattr(out, "tools_called", []) or []
            if not any(t in acceptable for t in called):
                return False
        return True


@dataclass
class MonotonicNarrowing(Evaluator):
    """`total_count` never increases across the turns flagged `narrows`.

    Only compares consecutive *narrowing* turns that both produced a numeric
    count; a turn that ran no search (None) is skipped without breaking the
    chain. A single narrowing turn trivially passes.
    """

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        outs, specs = _turns_out(ctx), _turns_spec(ctx)
        prev: int | None = None
        for spec, out in zip(specs, outs):
            if not spec.get("narrows"):
                continue
            count = getattr(out, "total_count", None)
            if count is None:
                continue
            if prev is not None and count > prev:
                return False
            prev = count
        return True


@dataclass
class FilterCarryOver(Evaluator):
    """After each turn, the rolling scope contains the turn's `expect_filters`.

    `ANY` asserts the key is present with any value; anything else asserts a
    value match. This is what proves the carry-over accumulated the right scope
    and (on a replacement pivot) overwrote the changed key.
    """

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        outs, specs = _turns_out(ctx), _turns_spec(ctx)
        for spec, out in zip(specs, outs):
            expected = spec.get("expect_filters") or {}
            if not expected:
                continue
            scope = getattr(out, "active_filters", {}) or {}
            for key, val in expected.items():
                if key not in scope:
                    return False
                if val is ANY or val == ANY:
                    continue
                if not _values_match(val, scope[key]):
                    return False
        return True


@dataclass
class NoStaleScope(Evaluator):
    """On a topic-switch turn, the search args must not carry a dropped value.

    Catches the stale-filter bug: after "commercial in Coimbatore instead", the
    turn's `search_auctions` call must not still pass `city="Chennai"` (nor a
    list containing it). No-op on turns without `forbid_tool_arg_values`.
    """

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        outs, specs = _turns_out(ctx), _turns_spec(ctx)
        for spec, out in zip(specs, outs):
            forbidden = spec.get("forbid_tool_arg_values") or {}
            if not forbidden:
                continue
            for call in getattr(out, "tool_calls", []) or []:
                args = call.get("args") or {}
                for key, bad in forbidden.items():
                    if key in args and _values_match(bad, args[key]):
                        return False
        return True
