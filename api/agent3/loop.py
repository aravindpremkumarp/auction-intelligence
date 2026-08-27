"""
api/agent3/loop.py
------------------
One turn: question in, answer plus the panel's rows out.

**Where the skill text goes, and why it matters.** Loaded skills are appended
to the *human* message, never spliced into the system prompt. The system
block is the cache prefix; the loop A/B measured the deep agent at 24% cache
overall and zero on the answer call, which made its whole cost column
unreadable. Keeping `instructions.md` byte-identical across turns is the
cheapest fix available, and it only works if nothing per-turn is mixed into
it. `test_agent3_agent.py::test_system_prompt_is_byte_identical_across_turns`
pins that.

**Memory is server-side.** The thread id keys a `Neo4jSaver` checkpoint, so
history survives a closed tab, a new browser, or a logout — and there is
exactly one place to clear it, unlike the client-carried scope object whose
four clear-sites leaked a previous conversation's filters into a new chat.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from api.agent3.agent import build_agent
from api.agent3.chatlog import record_turn as record_chatlog
from api.agent3.common import ToolSink, turn_context
from api.agent3.skills import USER_TEXT_DELIMITER, render_skills, select_skills
from api.observability import SLOW_AGENT_MS, record, timed

logger = logging.getLogger("api.agent3.loop")


@dataclass
class TurnResult:
    """What one turn produced.

    `panel_rows` never enters the transcript — see `ToolSink`.
    """
    answer: str
    auction_ids: list[str] = field(default_factory=list)
    panel_rows: list[dict] = field(default_factory=list)
    #: Web pages cited this turn. Rendered as source chips by the frontend's
    #: existing `extractWebSources`, which keys off an artifact named
    #: `internet_search` — so this needs no new UI.
    web_sources: list[dict] = field(default_factory=list)
    skills_loaded: list[str] = field(default_factory=list)
    model_calls: int = 0
    tool_calls: int = 0
    seconds: float = 0.0
    usage: dict = field(default_factory=dict)
    #: Repairs `AnswerGate` spent this turn (0 or 1). Surfaced rather than
    #: hidden: a turn that needed a repair cost an extra model call, and a
    #: rising rate here is the signal that the prompt — not the gate — needs
    #: work.
    gate_repairs: int = 0
    #: What the gate caught and made the model rewrite. Carried because the
    #: offending draft is deleted: without it a run reports "1 repair" and
    #: nobody can tell whether it caught a real invention or false-positived
    #: on a good answer.
    gate_repaired: list[str] = field(default_factory=list)
    #: `AnswerGate.inspect` re-run over the FINAL answer. `blocking` should be
    #: empty by construction (the gate would have repaired it); `advisory` is
    #: the numeric tier, which is recorded and never acted on. Reading this
    #: across real runs is the only evidence that could ever justify
    #: promoting that tier — see gates.py.
    gate_findings: dict = field(default_factory=dict)
    #: The TRUE match count, exact over every match. `len(panel_rows)` stops
    #: at PANEL_ROW_CAP, so a 812-match search has always displayed as 500.
    #: None means no search ran.
    total: int | None = None
    #: The last search's filters, in words, for the UI's query echo.
    query_echo: dict | None = None
    #: A `group_by` turn's distribution table. Without it such a turn reaches
    #: the UI looking identical to one that matched nothing.
    breakdown: list[dict] | None = None
    #: Whether a search ran at all, whatever it returned — "asked and got
    #: nothing" is an empty state worth rendering, "never asked" is not.
    searched: bool = False
    #: This turn's `TurnManifest` — what the UI should show for it. See
    #: api/agent3/manifest.py and docs/designs/turn-owned-property-cards.md.
    manifest: Any = None


def today_line(today: date | None = None) -> str:
    """Today's date, for the human message.

    **This is a fix, and it belongs here rather than in the system prompt.**
    `instructions.md` deliberately carries no date so the cache prefix stays
    byte-identical, and the consequence went unnoticed until the step-6 smoke
    run: nothing told the model what day it is, so it *guessed* whether an
    auction had happened. The same listing (748779, auction 4 May 2026) was
    called "still upcoming — it hasn't taken place yet" on one turn and
    "already past" on another, minutes apart. Both were confident; one was
    wrong, and telling a buyer an auction is still open when it closed months
    ago is exactly the shape of failure this design exists to prevent.

    Putting it in the human message keeps the prefix stable — the human
    message is per-turn unique already, so a value that changes daily costs
    nothing that was cacheable.
    """
    day = today or date.today()
    return (f"Today is {day.isoformat()}. Auction dates before this have "
            f"already happened; do not describe them as upcoming.")


def compose_input(question: str, skills_text: str,
                  today: date | None = None) -> str:
    """The human message: the date, then skill material, then the question.

    Material before question, so the question is the last thing the model
    reads and the thing it answers — reference text trailing the ask reads
    as an afterthought and gets treated like one.

    Everything before the LAST delimiter is ours, not the user's;
    `gates._latest_human_text` splits on exactly that to recover what the
    user actually wrote.
    """
    parts = [today_line(today)]
    if skills_text:
        parts.append(skills_text)
    parts.append(question)
    return USER_TEXT_DELIMITER.join(parts)


def _usage_of(turn_messages: list) -> dict:
    """Token usage summed over THIS TURN's model calls — no more, no less.

    Both ways of getting this wrong have now been made in this repo, in
    opposite directions:

    - **Summing the returned message list** re-charges history. With a
      checkpointer that list is the whole conversation, so turn 2 re-bills
      turn 1. The loop A/B reported 49,550 input tokens against an actual
      29,877 this way, and it read convincingly like "the transcript is
      getting expensive" — which was the claim under test.
    - **Taking only the final message** (what this function did first)
      undercounts the other direction: a turn that thinks, calls a tool and
      then answers makes three model calls, and only the last one is
      counted. On the smoke run's scope case that hid two calls out of
      three.

    The boundary that is actually correct is the tail since the last human
    message — this turn's calls, all of them, and nothing older.
    """
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
              "cached_input_tokens": 0}
    seen = False
    for m in turn_messages:
        if getattr(m, "type", "") != "ai":
            continue
        meta = getattr(m, "usage_metadata", None) or {}
        if not meta:
            continue
        seen = True
        details = meta.get("input_token_details") or {}
        totals["input_tokens"] += meta.get("input_tokens") or 0
        totals["output_tokens"] += meta.get("output_tokens") or 0
        totals["total_tokens"] += meta.get("total_tokens") or 0
        totals["cached_input_tokens"] += details.get("cache_read") or 0
    return totals if seen else {}


#: Sentinel: "you didn't say", as distinct from "explicitly no memory".
#: `checkpointer=None` is a legitimate request for a memoryless run (tests,
#: one-shot evals); omitting it must NOT be.
_DEFAULT = object()


def default_checkpointer():
    """A Neo4j-backed saver, imported lazily.

    Memory is opt-OUT here, and that is a correction. The first real-model
    smoke run asked a follow-up on the same thread and got back "this is the
    start of our conversation" — because `checkpointer` defaulted to None and
    nothing complained. A memoryless agent looks identical to a working one
    until someone asks a second question, and the transcript is the whole
    reason this design chose the deep loop's memory model over the tiered
    loop's summary. Wrong default; fixed.
    """
    from api.checkpointer import Neo4jSaver

    return Neo4jSaver()


async def run_turn(question: str, *, thread_id: str, model_name: str = "flash",
                   reasoning_effort: str | None = None,
                   checkpointer: Any = _DEFAULT, agent: Any = None) -> TurnResult:
    """Run one turn and return it in a UI-ready shape.

    `agent` is injectable so tests can drive a fake model without an
    OpenRouter key or a network call. `checkpointer` defaults to a real
    `Neo4jSaver`; pass `None` explicitly for a deliberately memoryless run.
    """
    started = time.perf_counter()
    sink = ToolSink()
    skills = select_skills(question)
    skills_text = render_skills(skills)

    if agent is None:
        saver = default_checkpointer() if checkpointer is _DEFAULT else checkpointer
        agent = build_agent(model_name=model_name,
                            reasoning_effort=reasoning_effort,
                            sink=sink, checkpointer=saver)

    # `turn_context` is what puts the thread id on every `agent3.tool` line
    # this turn emits, and what gives a *failed* turn something to report:
    # tools append to it as they run, so an exception out of `ainvoke` — which
    # returns no messages at all — still leaves a transcript of how far the
    # turn got. See common.TurnContext.
    with turn_context(thread_id) as turn:
        return await _run(
            question, skills=skills, skills_text=skills_text, sink=sink,
            agent=agent, turn=turn, thread_id=thread_id,
            model_name=model_name, reasoning_effort=reasoning_effort,
            started=started,
        )


async def _run(question: str, *, skills, skills_text: str, sink: ToolSink,
               agent: Any, turn, thread_id: str, model_name: str,
               reasoning_effort: str | None, started: float) -> TurnResult:
    """The turn itself, inside the context the tools read."""
    # The whole turn under one `timed` block, so latency and cost are one
    # line rather than two things a reader has to join by timestamp. It wraps
    # the accounting as well as the call because the token counts are only
    # knowable once the messages are back, and they belong on this line.
    with timed("agent3.turn", slow_ms=SLOW_AGENT_MS, model=model_name,
               thread=thread_id, effort=reasoning_effort) as obs:
        try:
            result = await agent.ainvoke(
                {"messages": [{"role": "user",
                               "content": compose_input(question, skills_text)}]},
                config={"configurable": {"thread_id": thread_id}},
            )
        except BaseException as exc:
            # BaseException, not Exception: a client that closes the tab
            # cancels this task, and a cancelled turn is one of the cases
            # worth being able to read back. Recorded, then re-raised
            # untouched — the caller's handling is unchanged.
            record_chatlog(
                thread_id=thread_id, model=model_name, question=question,
                live_steps=turn.steps, skills=[s.name for s in skills],
                seconds=round(time.perf_counter() - started, 2), error=exc,
            )
            raise

        messages = result.get("messages") or []
        final = messages[-1] if messages else None
        answer = getattr(final, "content", "") if final is not None else ""
        if isinstance(answer, list):  # some providers return content blocks
            answer = "".join(part.get("text", "") for part in answer
                             if isinstance(part, dict))

        # Count only THIS turn's calls. With a checkpointer the returned list
        # is the full conversation, so counting every AIMessage in it inflates
        # every turn after the first.
        turn_msgs = _messages_since_last_human(messages)
        model_calls = sum(1 for m in turn_msgs if getattr(m, "type", "") == "ai")
        tool_calls = sum(1 for m in turn_msgs if getattr(m, "type", "") == "tool")
        usage = _usage_of(turn_msgs)
        obs.update(
            model_calls=model_calls,
            tool_calls=tool_calls,
            skills=",".join(s.name for s in skills) or "-",
            answer_chars=len(answer or ""),
            gate_repairs=result.get("answer_gate_repairs") or 0,
            in_tok=usage.get("input_tokens", 0),
            cached_tok=usage.get("cached_input_tokens", 0),
            out_tok=usage.get("output_tokens", 0),
            total_tok=usage.get("total_tokens", 0),
        )

    _record_model_calls(turn_msgs, thread_id=thread_id, model=model_name)
    # The turn's text, on the same channel as its numbers. `question` and not
    # the composed input: the skill material and the date line are ours, and a
    # reader looking for what the user asked should not have to scroll past
    # them. See chatlog.py for the off switch.
    record_chatlog(
        thread_id=thread_id, model=model_name, question=question,
        answer=answer or "", turn_msgs=turn_msgs,
        skills=[s.name for s in skills],
        seconds=round(time.perf_counter() - started, 2),
    )

    turn_result = TurnResult(
        answer=answer or "",
        auction_ids=list(sink.auction_ids),
        panel_rows=list(sink.panel_rows),
        web_sources=list(sink.web_sources),
        skills_loaded=[s.name for s in skills],
        model_calls=model_calls,
        tool_calls=tool_calls,
        seconds=round(time.perf_counter() - started, 2),
        usage=usage,
        gate_repairs=result.get("answer_gate_repairs") or 0,
        gate_repaired=list(result.get("answer_gate_problems") or []),
        gate_findings=_gate_findings(answer or "", messages),
        total=sink.total,
        query_echo=sink.query_args,
        breakdown=sink.breakdown,
        searched=sink.searched,
    )

    # Built here rather than in the router because `messages` is the whole
    # checkpointed thread and it does not leave this function: the manifest's
    # turn ordinal and its id grounding both need it. `build_manifest` is
    # best-effort inside — a turn with a good answer never fails over the
    # metadata describing how to draw it.
    from api.agent3.manifest import build_manifest

    turn_result.manifest = await build_manifest(
        turn_result, messages, thread_id=thread_id)
    return turn_result


def _record_model_calls(turn_msgs: list, *, thread_id: str, model: str) -> None:
    """One line per model call in the turn, with its own token counts.

    The turn total says a turn cost 18k input tokens; it cannot say whether
    that was one call carrying a fat tool result or four calls re-reading the
    same prefix, and those two have opposite fixes. `cached_tok` per call is
    the number that tells them apart — cache discipline is the design
    constraint this agent is built around (see agent.py), and until now it was
    measurable only in an A/B script, never in production.

    `tools` names the calls the model asked for on that step, so a tool line
    can be traced back to the call that requested it.
    """
    index = 0
    for m in turn_msgs:
        if getattr(m, "type", "") != "ai":
            continue
        index += 1
        meta = getattr(m, "usage_metadata", None) or {}
        details = meta.get("input_token_details") or {}
        asked = [c.get("name") for c in (getattr(m, "tool_calls", None) or [])
                 if isinstance(c, dict) and c.get("name")]
        record("agent3.model_call", thread=thread_id, model=model, call=index,
               in_tok=meta.get("input_tokens") or 0,
               cached_tok=details.get("cache_read") or 0,
               out_tok=meta.get("output_tokens") or 0,
               tools=",".join(asked) or None)


def _gate_findings(answer: str, messages: list) -> dict:
    """Score the finished answer with the same checks the gate runs.

    Deliberately re-run here rather than smuggled out of the middleware.
    `after_model` sees a *draft*, and on a repaired turn the draft it rejected
    is not the answer anyone receives — reporting those findings would blame
    the delivered answer for a defect that was fixed. This scores what the
    caller actually got.
    """
    from api.agent3.gates import AnswerGate

    try:
        return AnswerGate().inspect(answer, messages)
    except Exception:  # noqa: BLE001 - reporting must never break a good turn
        logger.exception("answer gate inspection failed")
        return {}


def _messages_since_last_human(messages: list) -> list:
    """The tail belonging to the current turn."""
    for i in range(len(messages) - 1, -1, -1):
        if getattr(messages[i], "type", "") == "human":
            return messages[i + 1:]
    return list(messages)
