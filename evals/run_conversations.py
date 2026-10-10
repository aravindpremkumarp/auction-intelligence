"""
evals/run_conversations.py
--------------------------
Live multi-turn conversation runner. Plays each scripted conversation
(`evals/conversations.py`) through the tiered chat loop (`/chat/v2`),
threading its scope object between turns exactly as the v2 router does — so
the eval tests the real carry-over behavior, not a reimplementation of it.
Applies the conversation-level evaluators (trajectory, monotonic narrowing,
filter carry-over, no-stale-scope), prints a report, and exits non-zero when
the pass rate regresses below threshold. The binding is
`evals/tasks.py::conversation_v2`.

When ``LOGFIRE_TOKEN`` is set the run streams to Logfire like the single-turn
eval.

Usage (needs OpenRouter + Neo4j credentials in the environment):

    python -m evals.run_conversations

Env knobs:
    EVAL_MIN_CONVO_PASS    CI gate threshold (default 0.85)
    EVAL_CONVO_CONCURRENCY parallel conversations (default 2)
"""
from __future__ import annotations

import asyncio
import os
import sys

from pydantic_evals import Case, Dataset

from evals.conversation_evaluators import (
    CONVERSATION_GATES,
    ConversationOutput,
    ConversationTrajectory,
    FilterCarryOver,
    MonotonicNarrowing,
    NoStaleScope,
    PanelReferenceResolution,
    PanelState,
)
from evals.conversations import GOLDEN_CONVERSATIONS

MIN_CONVO_PASS = float(os.getenv("EVAL_MIN_CONVO_PASS", "0.85"))
CONCURRENCY = int(os.getenv("EVAL_CONVO_CONCURRENCY", "2"))
# Which logical chat model to eval ("flash"/"pro"); mirrors run_golden's knob.
CHAT_MODEL = os.getenv("EVAL_CHAT_MODEL", "flash")

_CONVERSATIONS_BY_ID = {c.conv_id: c for c in GOLDEN_CONVERSATIONS}


def _turn_spec(turn) -> dict:
    """Serialize a Turn's expectations into the metadata the evaluators read."""
    return {
        "expected_tools": turn.expected_tools,
        "narrows": turn.narrows,
        "expect_filters": turn.expect_filters,
        "topic_switch": turn.topic_switch,
        "forbid_tool_arg_values": turn.forbid_tool_arg_values,
        "expect_panel": turn.expect_panel,
        "references_panel": turn.references_panel,
    }


def build_conversation_dataset() -> Dataset:
    cases = [
        Case(
            name=c.conv_id,
            inputs=c.conv_id,
            metadata={
                "description": c.description,
                "turns": [_turn_spec(t) for t in c.turns],
            },
        )
        for c in GOLDEN_CONVERSATIONS
    ]
    evaluators = [
        ConversationTrajectory(),
        MonotonicNarrowing(),
        FilterCarryOver(),
        NoStaleScope(),
        PanelState(),
        PanelReferenceResolution(),
    ]
    return Dataset(name="golden-conversations", cases=cases, evaluators=evaluators)


async def _run_conversation(conv_id: str) -> ConversationOutput:
    """Play one scripted conversation through the agent under test.

    The binding itself lives in `evals/tasks.py`, shared with the golden
    runner.
    """
    from evals.tasks import conversation_task

    return await conversation_task()(_CONVERSATIONS_BY_ID[conv_id])


async def main() -> int:
    from api.telemetry import configure_telemetry

    configure_telemetry()

    dataset = build_conversation_dataset()
    report = await dataset.evaluate(
        _run_conversation,
        name="golden-conversations",
        max_concurrency=CONCURRENCY,
    )
    report.print(include_input=True, include_output=False)

    # CI gate: a conversation passes only when ALL gating assertions pass.
    total = len(report.cases)
    passed = 0
    for c in report.cases:
        if all(
            (r := c.assertions.get(k)) is not None and r.value
            for k in CONVERSATION_GATES
        ):
            passed += 1
    from evals.run_golden import report_cost

    report_cost(report.cases, label=" (summed across turns)")

    rate = passed / total if total else 0.0
    print(
        f"\nConversation pass rate (all gates): {passed}/{total} = {rate:.1%} "
        f"(threshold {MIN_CONVO_PASS:.0%})"
    )
    if rate < MIN_CONVO_PASS:
        print(
            f"REGRESSION: conversation pass rate {rate:.1%} below "
            f"threshold {MIN_CONVO_PASS:.0%}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
