"""
api/chat/deep/loop.py
---------------------
One turn on the Deep Agents harness.

    user message -> intent gate -> graph (think -> tools -> think -> ...)
                 -> answer gate -> the same TurnResult the tiered loop returns

**The contract is the point.** `TurnResult` here is field-compatible with
`api/chat/v2/loop.py::TurnResult`, so `build_artifacts`, `panel_sync_ids`,
`_build_response` and the whole matches-panel path in `web/app.js` work
against either loop unchanged. Switching loops on /lab is a URL change, not a
frontend port — and the A/B compares two loops through one response shape
rather than two shapes that might each be wrong differently.

**Memory is the difference.** The tiered loop carries a `scope` object: the
active filters, the last ids, and (since the "these areas" fix) the previous
question and the names the last answer used. That is a *summary*, and a
summary can only answer questions about what it chose to summarise. Here the
memory is the transcript itself, checkpointed in Neo4j under the conversation
id, so a follow-up can refer to anything that was actually said. The cost is
the thing the tiered loop was built to avoid: every earlier message is re-sent
and re-billed on every turn. `docs/chat-loop-ab-2026-08.md` carries the
measurement.

`scope` is still harvested and returned, because the matches panel reads
`last_ids`. Nothing in this loop consumes it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Wall-clock ceiling for one turn. A ReAct loop can wander, and the browser's
#: idle guard gives up at 75 s; failing here with a real message beats the
#: client timing out on a turn that is still running.
TURN_TIMEOUT_S = 120.0


@dataclass
class TurnResult:
    """Field-compatible with the tiered loop's TurnResult — see module doc."""

    answer: str = ""
    recommendation: Any = None
    executed: list[Any] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
    last_total_count: int | None = None
    last_ids: list[str] = field(default_factory=list)
    last_question: str = ""
    last_entities: dict[str, list[str]] = field(default_factory=dict)
    #: Always 0 — the deep loop has no tiers. Kept so the response schema and
    #: the obs log are identical across both loops.
    tier: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    seconds: float = 0.0
    gate: Any = None
    #: Graph supersteps used, for the A/B. No tiered-loop equivalent.
    steps: int = 0


async def run_turn(
    question: str,
    *,
    thread_id: str,
    model_name: str = "flash",
    reasoning_effort: str | None = None,
    checkpointer: Any = None,
    on_event: Callable[[str, dict], None] | None = None,
) -> TurnResult:
    """Run one chat turn on the deep loop. Never raises for a tool failure."""
    import asyncio

    from api.chat.deep.agent import (
        RECURSION_LIMIT,
        ToolSink,
        build_deep_agent,
    )
    from api.chat.v2.middleware import (
        check_answer,
        classify_intent,
        wrap_pasted_content,
    )
    from api.chat.v2.scope import harvest_scope

    started = time.perf_counter()
    result = TurnResult(last_question=question)

    def emit(event: str, payload: dict) -> None:
        if on_event is not None:
            on_event(event, payload)

    # Same gate, same place in the order as the tiered loop: refuse
    # harvesting-shaped requests before spending anything on them. In code
    # rather than in the prompt because a prompt-resident policy loses
    # arguments with the next prompt edit.
    intent = classify_intent(question)
    if not intent:
        logger.info("chat deep: intent gate refused (%s)", intent.reason)
        result.answer = intent.refusal
        result.last_question = ""
        result.seconds = round(time.perf_counter() - started, 2)
        return result

    question = wrap_pasted_content(question)

    sink = ToolSink()
    usage = _UsageTap()
    agent = build_deep_agent(
        sink=sink, model_name=model_name, reasoning_effort=reasoning_effort,
        checkpointer=checkpointer,
    )

    emit("status", {"label": "Thinking…"})
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": RECURSION_LIMIT,
        "callbacks": [usage],
    }
    try:
        state = await asyncio.wait_for(
            agent.ainvoke({"messages": [("user", question)]}, config=config),
            timeout=TURN_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("chat deep: turn exceeded %.0fs", TURN_TIMEOUT_S)
        result.executed = list(sink.calls)
        result.answer = _TIMED_OUT
        usage.apply(result)
        result.seconds = round(time.perf_counter() - started, 2)
        return result
    except Exception:
        logger.exception("chat deep: graph failed")
        result.executed = list(sink.calls)
        result.answer = _FAILED
        usage.apply(result)
        result.seconds = round(time.perf_counter() - started, 2)
        return result

    messages = state.get("messages") or []
    result.executed = list(sink.calls)
    result.answer = _final_answer(messages) or _NO_ANSWER
    result.steps = len(messages)
    # From the tap, NOT by summing `messages`. With a checkpointer the
    # returned state is the WHOLE conversation, so summing its usage metadata
    # re-counts every earlier turn: turn 3 reports turns 1+2+3 and the growth
    # looks like the transcript getting expensive when it is really the same
    # tokens counted three times. The tap sees only the calls this invocation
    # made. Caught by tracing a live two-turn conversation, where turn 2
    # reported 49,550 input tokens against an actual 29,877.
    usage.apply(result)

    # Report-only, exactly as in the tiered loop — the fire rate is the
    # measurement that decides whether enforcement is affordable, and it has
    # to be comparable across both loops to be worth anything.
    result.gate = check_answer(
        result.answer,
        [call.result for call in result.executed],
        recommendation=None,
        extra_numbers=_numeric_args(result.executed),
        # The transcript IS the memory here, so every id this conversation has
        # ever surfaced is legitimately citable — not just the last turn's.
        extra_ids=_all_ids(result.executed),
    )

    # For the matches panel only, which reads `last_ids` and `last_total_count`.
    #
    # `last_entities` is deliberately NOT harvested here. It exists because the
    # tiered loop's summary could not resolve "these areas" without it, and
    # that loop feeds it back into its next prompt. This loop resolves the same
    # reference from the checkpointed transcript, so computing the name list
    # would be work whose only consumer is a loop that isn't running. The field
    # stays on TurnResult (the response schema is shared) and stays empty.
    executed_dicts = [call.as_dict() for call in result.executed]
    result.filters, harvested_total, harvested_ids = harvest_scope(executed_dicts)
    if harvested_total is not None:
        result.last_total_count = harvested_total
    if harvested_ids:
        result.last_ids = harvested_ids

    result.seconds = round(time.perf_counter() - started, 2)
    return result


_TIMED_OUT = (
    "That question took longer than I can hold the line for. Try narrowing it "
    "— a city, a bank, or a price range — and I'll get there faster."
)
_FAILED = "Something broke while I was working on that. Please try again."
_NO_ANSWER = (
    "I ran the searches but couldn't write them up. Please try that again."
)


def _final_answer(messages: list[Any]) -> str:
    """The last AI message carrying real text.

    Walks backwards past tool-call-only AI messages, whose `content` is empty
    (or a content-block list) because the model was calling a tool rather than
    talking. Taking `messages[-1]` blindly returns "" on exactly those turns.
    """
    for message in reversed(messages):
        if getattr(message, "type", "") != "ai":
            continue
        content = getattr(message, "content", "")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            text = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
            if text:
                return text
    return ""


class _UsageTap:
    """Records model usage as each call finishes. The turn's only usage source.

    Reading it off the returned messages instead — the way the tiered loop
    does, because its state is one turn — is wrong here in both directions:

    * On a turn that **finishes**, the checkpointed state is the whole
      conversation, so summing it re-counts every earlier turn. Traced live:
      turn 2 of a two-turn chat reported 49,550 input tokens against an
      actual 29,877, and the over-count grows with every turn.
    * On a turn that **times out or raises**, `ainvoke` never returns, so
      there are no messages at all and the turn reads as zero tokens. It did
      not cost zero — a turn that burns the full ceiling and is then
      abandoned is the most expensive kind there is, and reporting it as free
      tells the A/B that the loop failing most is the loop spending least.

    Counting the calls as they happen is right on both.

    A plain object rather than a `BaseCallbackHandler` subclass so that
    importing this module does not pull LangChain in — see the RSS note in
    `docs/chat-loop-ab-2026-08.md`. LangChain duck-types callbacks, and the
    `ignore_*` properties are what its dispatcher checks before calling.
    """

    ignore_llm = False
    ignore_chat_model = False
    ignore_chain = True
    ignore_agent = True
    ignore_retriever = True
    ignore_retry = True
    ignore_custom_event = True
    raise_error = False
    run_inline = False

    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0

    def __getattr__(self, name: str) -> Any:
        """Absorb every `on_*` hook we do not implement."""
        if name.startswith("on_"):
            return lambda *a, **kw: None
        raise AttributeError(name)

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        for generations in getattr(response, "generations", None) or []:
            for generation in generations or []:
                message = getattr(generation, "message", None)
                meta = getattr(message, "usage_metadata", None)
                if not meta:
                    continue
                self.calls += 1
                self.input_tokens += meta.get("input_tokens", 0) or 0
                self.output_tokens += meta.get("output_tokens", 0) or 0
                details = meta.get("input_token_details") or {}
                self.cached_tokens += details.get("cache_read", 0) or 0

    def apply(self, result: TurnResult) -> None:
        """Copy what was spent onto a turn that has no messages to read."""
        result.model_calls = self.calls
        result.input_tokens = self.input_tokens
        result.output_tokens = self.output_tokens
        result.cached_tokens = self.cached_tokens


def _numeric_args(executed: list[Any]) -> list[float]:
    """Filter thresholds the user asked for, from the args that actually ran.

    Mirrors `v2/loop.py::_numeric_args`: "under Rs 40 Lakhs" in an answer is
    the threshold the user just named, not a fabricated number, so the gate
    must count it as grounded.
    """
    out: list[float] = []
    for call in executed:
        for value in (call.args or {}).values():
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                out.append(float(value))
    return out


def _all_ids(executed: list[Any]) -> list[str]:
    """Every auction_id this turn's tools returned, model-visible or not.

    Includes `ui_rows`: the answer may cite a row the panel holds, and the
    tiered loop's gate had the same blind spot until carried ids were added
    to it.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for call in executed:
        rows: list[Any] = []
        if isinstance(call.result, dict):
            rows.extend(call.result.get("results") or [])
        rows.extend(call.ui_rows or [])
        for row in rows:
            if not isinstance(row, dict):
                continue
            value = row.get("auction_id")
            if value is None:
                continue
            text = str(value)
            if text not in seen:
                seen.add(text)
                ids.append(text)
    return ids
