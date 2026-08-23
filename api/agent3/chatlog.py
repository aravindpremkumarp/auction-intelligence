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

**Off switch and limits, because this is user text leaving the box.**
``AGENT3_CHATLOG=0`` disables it outright; ``AGENT3_CHATLOG_MAX_CHARS``
(default 4000) caps the question and the answer, and each tool payload gets a
quarter of that. Clipped values say how much was dropped rather than pretending
they are whole.
"""
from __future__ import annotations

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


def record_turn(*, thread_id: str, model: str, question: str, answer: str,
                turn_msgs: list, skills: list[str] | None = None,
                seconds: float | None = None) -> None:
    """Emit one `agent3.chatlog` line carrying this turn's text.

    Never raises: a transcript that fails to serialise must not take down a
    turn that already succeeded. It logs the failure and returns, which costs
    one transcript instead of one answer.
    """
    if not enabled():
        return
    try:
        limit = _max_chars()
        rows = steps(turn_msgs, limit=limit // 4)
        record_text(
            "agent3.chatlog",
            thread=thread_id, model=model,
            skills=",".join(skills or []) or "-",
            tools=",".join(r["tool"] for r in rows) or "-",
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
