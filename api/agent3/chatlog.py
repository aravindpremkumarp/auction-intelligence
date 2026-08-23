"""
api/agent3/chatlog.py
---------------------
The turn's *words*, shipped to Logfire alongside its numbers.

**Why this exists.** Everything agent3 recorded until now was accounting —
tokens, call counts, latency, tool names. Ask "what did the user actually
ask, and what did we answer?" and the honest reply was: read it out of the
Neo4j checkpoint, blob by base64 blob. The one place that already holds every
turn from every environment, and that Claude can query directly through the
Logfire connector, held no text at all. So a bad answer could be *counted*
but never *read*, which is the half that matters when someone reports the
agent got something wrong.

**Why not turn on HTTPX body capture instead.** That would dump the whole
prompt — the system block, the skill text, the full replayed history — on
every one of the four-plus model calls a turn makes, most of it byte-identical
to the last turn's. This records the turn once, in the shape a reader wants:
question, answer, and the tool steps in between.

**A failed turn is recorded too.** `error` and the live step list from
`common.TurnContext` mean a turn that raised — a model timeout, a cancelled
stream — still leaves a readable line, with `outcome` saying which of ok /
error / cancelled it was. Without it the only turns with no transcript were
the ones someone would actually go looking for.

**Off switch and limits, because this is user text leaving the box.**
``AGENT3_CHATLOG=0`` disables it outright; ``AGENT3_CHATLOG_MAX_CHARS``
(default 4000) caps the question and the answer, and each tool payload gets a
quarter of that. Clipped values say how much was dropped rather than pretending
they are whole.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from api.observability import record_text

logger = logging.getLogger("api.agent3.chatlog")

#: Per-field ceiling. Generous enough that a normal answer survives intact
#: (the agent's answers run ~1–2k chars), small enough that one pathological
#: turn cannot dominate the export.
_DEFAULT_MAX_CHARS = 4000


def enabled() -> bool:
    """False when an operator has switched transcript capture off."""
    return os.getenv("AGENT3_CHATLOG", "1").strip().lower() not in {
        "0", "false", "no", "off"}


def _max_chars() -> int:
    try:
        return max(200, int(os.getenv("AGENT3_CHATLOG_MAX_CHARS",
                                      str(_DEFAULT_MAX_CHARS))))
    except ValueError:
        return _DEFAULT_MAX_CHARS


def clip(value: Any, limit: int) -> str:
    """`value` as text, truncated with a count of what was dropped.

    The suffix is the point: a silently truncated answer reads as a complete
    one that ended oddly, and someone debugging it would chase the model for
    a defect this function introduced.
    """
    text = value if isinstance(value, str) else _stringify(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… (+{len(text) - limit} chars)"


def _stringify(value: Any) -> str:
    """Non-string payloads (tool args, content blocks) as compact JSON."""
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _text_of(content: Any) -> str:
    """Message content as plain text, flattening provider content blocks."""
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict))
    return content if isinstance(content, str) else _stringify(content)


def steps(turn_msgs: list, *, limit: int) -> list[dict]:
    """The tool round-trips of one turn, in order, as plain dicts.

    Args are taken from the AI message that *requested* the call and matched
    to the result by `tool_call_id`, because the tool message carries only the
    result. Unmatched results still appear — a step with no recoverable args
    is worth more to a reader than a gap in the transcript.
    """
    asked: dict[str, dict] = {}
    out: list[dict] = []
    for m in turn_msgs:
        kind = getattr(m, "type", "")
        if kind == "ai":
            for call in getattr(m, "tool_calls", None) or []:
                if isinstance(call, dict) and call.get("id"):
                    asked[call["id"]] = call
        elif kind == "tool":
            call = asked.get(getattr(m, "tool_call_id", None) or "", {})
            out.append({
                "tool": getattr(m, "name", None) or call.get("name") or "?",
                "args": clip(call.get("args"), limit),
                "result": clip(_text_of(getattr(m, "content", "")), limit),
            })
    return out


def _outcome_of(error: BaseException | None) -> str:
    """`ok`, `cancelled` or `error` — the three ways a turn ends.

    Cancellation is called out rather than lumped in with failure because it
    is usually not one: the streaming endpoint cancels the turn when the
    browser goes away, and counting those as errors would invent an error
    rate out of people closing tabs.
    """
    if error is None:
        return "ok"
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    return "error"


def record_turn(*, thread_id: str, model: str, question: str, answer: str = "",
                turn_msgs: list | None = None,
                live_steps: list[dict] | None = None,
                skills: list[str] | None = None,
                seconds: float | None = None,
                error: BaseException | None = None) -> None:
    """Emit one `agent3.chatlog` line carrying this turn's text.

    **A turn that raised gets a line too, and that is the point of `error`
    and `live_steps`.** When `ainvoke` throws there is no message list, so
    the tool round-trips the transcript is built from never exist — the turns
    hardest to explain were the only ones leaving nothing to read. Tools
    record themselves as they run (`common.TurnContext`), so a failed turn
    still reports the question, how far it got, and what broke. Those step
    results are size summaries rather than payloads; the model never got to
    use them anyway.

    Never raises: a transcript that fails to serialise must not take down a
    turn that already succeeded. It logs the failure and returns, which costs
    one transcript instead of one answer.
    """
    if not enabled():
        return
    try:
        limit = _max_chars()
        rows = (steps(turn_msgs, limit=limit // 4) if turn_msgs
                else list(live_steps or []))
        record_text(
            "agent3.chatlog",
            thread=thread_id, model=model,
            outcome=_outcome_of(error),
            err=f"{type(error).__name__}: {error}" if error is not None else None,
            skills=",".join(skills or []) or "-",
            tools=",".join(str(r.get("tool", "?")) for r in rows) or "-",
            q_chars=len(question or ""), a_chars=len(answer or ""),
            seconds=seconds,
            texts={
                "question": clip(question or "", limit),
                "answer": clip(answer or "", limit),
                # One attribute rather than a column per step: the step count
                # varies per turn, and Logfire indexes attributes, not shapes.
                "steps_json": clip(_stringify(rows), limit * 2),
            },
        )
    except Exception:  # noqa: BLE001 - observability must never break a turn
        logger.exception("agent3 chatlog record failed")
