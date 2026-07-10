"""
evals/run_conversations.py
--------------------------
Live multi-turn conversation runner. Plays each scripted conversation
(`evals/conversations.py`) through the real chat agent, threading message
history and re-deriving the rolling scope between turns EXACTLY as the /chat
router does — so the eval tests the real carry-over behavior, not a
reimplementation of it. Applies the conversation-level evaluators (trajectory,
monotonic narrowing, filter carry-over, no-stale-scope), prints a report, and
exits non-zero when the pass rate regresses below threshold.

State threading per turn mirrors `api/chat/router.py::_prepare_turn`:
  * the previous turn's `result.all_messages()` becomes the next
    `message_history`;
  * `_extract_active_filters()` (imported from the router, not re-implemented)
    rebuilds the rolling scope + last total_count, which seed the next
    `ChatDeps` so the agent gets the same "Active search scope" block it would
    in production.

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
    TurnOutput,
)
from evals.conversations import GOLDEN_CONVERSATIONS

MIN_CONVO_PASS = float(os.getenv("EVAL_MIN_CONVO_PASS", "0.85"))
CONCURRENCY = int(os.getenv("EVAL_CONVO_CONCURRENCY", "2"))

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
    """Play one scripted conversation through the real agent, capturing per-turn
    tools, args, match count, the re-derived rolling scope, and the derived UI
    matches panel.

    The panel is threaded exactly like production: the browser's current panel
    ids ride into `ChatDeps.panel_auction_ids` each turn, and after the turn the
    panel becomes (in priority order) the citation-driven sync
    (`panel_sync_ids`), else this turn's last search-shaped artifact
    (`turn_panel_ids` — what the frontend renders), else unchanged. Both
    functions are imported from `api/chat/panel.py`, not re-implemented."""
    from pydantic_ai.messages import ToolCallPart

    from api.agent import ChatDeps, agent
    from api.chat.panel import panel_sync_ids, turn_panel_ids
    from api.chat.router import _extract_active_filters, _tool_returns

    convo = _CONVERSATIONS_BY_ID[conv_id]
    history = None
    active_filters: dict = {}
    last_total: int | None = None
    panel: list[str] = []
    turns_out: list[TurnOutput] = []

    for turn in convo.turns:
        deps = ChatDeps(
            active_filters=active_filters or None,
            last_total_count=last_total,
            panel_auction_ids=panel or None,
        )
        result = await agent.run(turn.message, message_history=history, deps=deps)
        history = result.all_messages()
        # Re-derive the rolling scope + last count the SAME way the router does,
        # so the next turn's deps match production.
        active_filters, last_total = _extract_active_filters(history)

        seen: set[str] = set()
        tools: list[str] = []
        calls: list[dict] = []
        for msg in result.new_messages():
            for part in getattr(msg, "parts", []):
                if isinstance(part, ToolCallPart):
                    try:
                        args = part.args_as_dict()
                    except Exception:  # noqa: BLE001 - malformed args → empty
                        args = {}
                    calls.append({"tool": part.tool_name, "args": args})
                    if part.tool_name not in seen:
                        seen.add(part.tool_name)
                        tools.append(part.tool_name)

        # Derive the panel after this turn with the real sync logic.
        answer = result.output or ""
        panel_before = list(panel)
        turn_returns = _tool_returns(result.new_messages())
        all_returns = _tool_returns(history)
        synced = panel_sync_ids(answer, turn_returns, all_returns, panel_before)
        panel = synced or turn_panel_ids(turn_returns) or panel_before

        turns_out.append(TurnOutput(
            message=turn.message,
            answer=answer,
            tools_called=tools,
            tool_calls=calls,
            total_count=last_total,
            active_filters=dict(active_filters),
            panel_ids_before=panel_before,
            panel_ids=list(panel),
        ))

    return ConversationOutput(turns=turns_out)


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
