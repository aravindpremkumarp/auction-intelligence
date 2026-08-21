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

import time
from dataclasses import dataclass, field
from typing import Any

from api.agent3.agent import build_agent
from api.agent3.common import ToolSink
from api.agent3.skills import render_skills, select_skills


@dataclass
class TurnResult:
    """What one turn produced.

    `panel_rows` never enters the transcript — see `ToolSink`.
    """
    answer: str
    auction_ids: list[str] = field(default_factory=list)
    panel_rows: list[dict] = field(default_factory=list)
    skills_loaded: list[str] = field(default_factory=list)
    model_calls: int = 0
    tool_calls: int = 0
    seconds: float = 0.0
    usage: dict = field(default_factory=dict)


def compose_input(question: str, skills_text: str) -> str:
    """The human message: skill material first, then the question.

    Material before question, so the question is the last thing the model
    reads and the thing it answers — reference text trailing the ask reads
    as an afterthought and gets treated like one.
    """
    if not skills_text:
        return question
    return f"{skills_text}\n\n---\n\n{question}"


def _usage_of(message: Any) -> dict:
    """Token usage off the final AI message only.

    NOT summed over the returned message list: with a checkpointer that list
    is the whole conversation, so summing re-charges turn 1 on turn 2 and
    turn 1+2 on turn 3. That exact bug reported 49,550 input tokens against
    an actual 29,877 in the loop A/B, and read convincingly like "the
    transcript is getting expensive" — which was the claim under test.
    """
    meta = getattr(message, "usage_metadata", None) or {}
    if not meta:
        return {}
    details = meta.get("input_token_details") or {}
    return {
        "input_tokens": meta.get("input_tokens"),
        "output_tokens": meta.get("output_tokens"),
        "total_tokens": meta.get("total_tokens"),
        "cached_input_tokens": details.get("cache_read"),
    }


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

    result = await agent.ainvoke(
        {"messages": [{"role": "user",
                       "content": compose_input(question, skills_text)}]},
        config={"configurable": {"thread_id": thread_id}},
    )

    messages = result.get("messages") or []
    final = messages[-1] if messages else None
    answer = getattr(final, "content", "") if final is not None else ""
    if isinstance(answer, list):  # some providers return content blocks
        answer = "".join(part.get("text", "") for part in answer
                         if isinstance(part, dict))

    # Count only THIS turn's calls. With a checkpointer the returned list is
    # the full conversation, so counting every AIMessage in it inflates every
    # turn after the first.
    turn_msgs = _messages_since_last_human(messages)
    model_calls = sum(1 for m in turn_msgs if getattr(m, "type", "") == "ai")
    tool_calls = sum(1 for m in turn_msgs if getattr(m, "type", "") == "tool")

    return TurnResult(
        answer=answer or "",
        auction_ids=list(sink.auction_ids),
        panel_rows=list(sink.panel_rows),
        skills_loaded=[s.name for s in skills],
        model_calls=model_calls,
        tool_calls=tool_calls,
        seconds=round(time.perf_counter() - started, 2),
        usage=_usage_of(final),
    )


def _messages_since_last_human(messages: list) -> list:
    """The tail belonging to the current turn."""
    for i in range(len(messages) - 1, -1, -1):
        if getattr(messages[i], "type", "") == "human":
            return messages[i + 1:]
    return list(messages)
