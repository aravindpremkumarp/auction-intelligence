"""
api/agent3/manifest.py
----------------------
One record per assistant turn, describing what the UI should show for it.

Designed in `docs/designs/turn-owned-property-cards.md` (#404) and the reason
it exists is one sentence from that doc: *every agent surface shows matches,
not recommendations, and the matches panel loses sync with the conversation.*

The fix is structural rather than cosmetic. Today the browser rebuilds a
turn's cards from whatever artifacts it still holds, so scrolling back shows
the wrong set and a reload shows whatever the client-saved copy kept. A
manifest is written once, server-side, next to the thread — so the cards for
turn 3 are the cards turn 3 produced, by construction, with no synchronising
code anywhere because there is no shared mutable panel to synchronise.

**The layering rule this module obeys, verbatim from the design:**

    LLM text = explanation.
    ToolSink/manifest = UI structure.
    Neo4j = authoritative property facts.
    Never parse the agent's prose to determine which cards to display.

That last line is worth being precise about, because this module does read
the answer. *Which rows exist* comes only from the sink. *Which of those rows
the agent talked about* comes from the answer, via the same guarded extraction
the AnswerGate already uses to catch hallucinated ids. An id in the prose that
is in no tool output is a gate matter and never becomes a card.

Pure functions only — the Neo4j read/write lives in `manifest_store.py`, so
every rule here is testable without a database.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from api.agent3.common import guarded_ids, message_text, tool_output_text

logger = logging.getLogger("api.agent3.manifest")

#: The card fields the UI renders, and nothing else. #404 is explicit that
#: `card_rows` is a *snapshot of card fields*, not full graph rows: the
#: duplication is deliberate — it freezes what the user was actually shown
#: even if the listing changes later — and bounding it is what keeps that
#: duplication affordable. `notice_lot_count` and `area_sqft_scope` are in
#: here because the scope badge is a correctness feature, not decoration: a
#: lot fact on a multi-lot notice must never render as the property's own.
CARD_FIELDS = (
    "auction_id", "title", "city", "area", "district",
    "bank", "asset_category", "property_types", "auction_type",
    "reserve_price", "emd", "auction_start", "application_deadline", "url",
    "notice_lot_count", "area_sqft", "area_sqft_scope",
    "notice_area_sqft_range", "auction_attempt", "attempt_scope",
    "possession",
)

#: Cap on ids fetched for a `detail` turn's cards. Mirrors
#: `artifacts.MAX_FALLBACK_IDS` for the same reason: a turn naming more
#: listings than this ran a search, and a search fills the sink.
MAX_DETAIL_IDS = 25

#: Sentence boundary. Digits and ₹ and ( are in the lookahead ON PURPOSE — a
#: unit that starts with an id must split away from the one before it, or the
#: previous property's price gets attributed to this one. Decimals are safe
#: without a guard because `₹1.5` has no whitespace after its dot.
#:
#: Known imperfection, accepted in #404 and repeated here so nobody files it
#: as a bug: `"748779 costs Rs. 50 lakh"` splits after `Rs.`, leaving the
#: clipped unit `"748779 costs Rs."`. A clipped quote on a card is a cosmetic
#: loss; a quote attached to the wrong property would not be, and that is the
#: trade this regex makes.
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9₹(])")

#: Markdown list items and headings are their own units regardless of
#: punctuation — "- 748779: symbolic possession" has no sentence-ending mark
#: and would otherwise swallow the whole list.
_BLOCK_START = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|#{1,6}\s|>\s)")


@dataclass
class TurnManifest:
    """What one assistant turn produced, in the shape the UI needs.

    One per *final* answer, without exception — a greeting and a refusal get
    one too (`kind="none"`), because ordinals and manifests must never
    disagree about how many turns a thread has.
    """

    thread_id: str
    turn_index: int
    #: search | detail | distribution | empty | none. See `_kind_of`.
    kind: str = "none"
    card_rows: list[dict] = field(default_factory=list)
    discussed_ids: list[str] = field(default_factory=list)
    annotations: dict[str, list[str]] = field(default_factory=dict)
    query_echo: dict | None = None
    counts: dict = field(default_factory=lambda: {"total": 0, "shown": 0})
    breakdown: list[dict] | None = None
    web_sources: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def turn_index_of(messages: list) -> int:
    """How many answers the user has actually received, this one included.

    Counting *final* assistant messages, not all of them: a tool-using turn
    checkpoints several AIMessages and a gate repair inserts more, so a naive
    count runs ahead of the conversation. The finality test is the one
    `AnswerGate.after_model` uses — an AI message with no tool calls — so the
    two can never disagree about where a turn ended.

    1-based and inclusive: at write time the current answer is already in the
    list, so the first turn's manifest is index 1. The frontend counts final
    assistant messages the same way on reload, which is what makes the join
    safe without sending an ordinal the client could get wrong.
    """
    count = 0
    for m in messages or []:
        if getattr(m, "type", "") != "ai":
            continue
        if getattr(m, "tool_calls", None):
            continue
        count += 1
    return count


def split_units(text: str) -> list[str]:
    """The answer, split into quotable units.

    Per line, so a markdown list is a list of units rather than one run-on
    sentence, and so a heading cannot annex the paragraph under it.
    """
    units: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _BLOCK_START.match(line):
            units.append(stripped)
            continue
        units.extend(u.strip() for u in _SENTENCE.split(stripped) if u.strip())
    return units


def annotate(answer: str, eligible: set[str]) -> dict[str, list[str]]:
    """The agent's own sentence(s) about each property it discussed.

    This is the recommendation. Not a summary of it, not a model call to
    produce one — the verbatim text, so the card cannot disagree with the chat
    above it, because it *is* the chat above it. Zero extra tokens, zero
    latency.

    A unit naming two ids attaches to both. A unit naming none attaches to
    nothing. An id the answer names but no tool output grounds is dropped
    here: that is a gate escape, and inventing a card for it would be the
    display layer laundering a hallucination.
    """
    out: dict[str, list[str]] = {}
    for unit in split_units(answer):
        for token in guarded_ids(unit):
            if token not in eligible:
                continue
            out.setdefault(token, []).append(unit)
    return out


def project_card(row: dict) -> dict:
    """One row, reduced to the card fields — see CARD_FIELDS."""
    return {k: row[k] for k in CARD_FIELDS if k in row and row[k] is not None}


def _kind_of(result, discussed: list[str]) -> str:
    """Which of the five shapes this turn is.

    The distinction that matters most is `distribution` vs `empty`: a
    `group_by` turn matched plenty and grouped it, and rendering that as "0
    matches" would be false. `none` covers a greeting, a refusal, or a
    web-only answer — every one of which still consumes a turn index.
    """
    if getattr(result, "breakdown", None):
        return "distribution"
    if getattr(result, "panel_rows", None):
        return "search"
    if getattr(result, "searched", False):
        return "empty"
    return "detail" if discussed else "none"


async def build_manifest(result, messages: list, *, thread_id: str) -> TurnManifest:
    """Build the manifest for a finished turn.

    Best-effort by contract: a manifest is how the turn is *displayed*, and a
    failure here must never fail a turn that already has a good answer. Same
    rule `artifacts.py` runs under, and it has earned its place.
    """
    answer = getattr(result, "answer", "") or ""
    manifest = TurnManifest(thread_id=thread_id,
                            turn_index=turn_index_of(messages))
    try:
        # Grounding is thread-wide on purpose: on a follow-up the agent names
        # ids an earlier turn's search returned, and scoping this to the
        # current turn would strip the card off every one of them.
        evidence = tool_output_text(messages)
        named = guarded_ids(answer)
        eligible = {t for t in named if t in evidence}
        dropped = [t for t in named if t not in eligible]
        if dropped:
            # A gate escape worth counting, not worth failing over.
            logger.warning("manifest dropped ungrounded ids: %s thread=%s",
                           ",".join(dropped), thread_id)

        manifest.discussed_ids = [t for t in named if t in eligible]
        manifest.annotations = annotate(answer, eligible)
        manifest.kind = _kind_of(result, manifest.discussed_ids)
        manifest.query_echo = getattr(result, "query_echo", None)
        manifest.breakdown = getattr(result, "breakdown", None)
        manifest.web_sources = list(getattr(result, "web_sources", None) or [])

        rows = list(getattr(result, "panel_rows", None) or [])
        if not rows and manifest.kind == "detail":
            rows = await _detail_rows(manifest.discussed_ids)
        manifest.card_rows = [project_card(r) for r in rows]

        total = getattr(result, "total", None)
        manifest.counts = {
            "total": len(manifest.card_rows) if total is None else int(total),
            "shown": len(manifest.card_rows),
        }
    except Exception:  # noqa: BLE001 - display metadata, never fatal
        logger.exception("manifest build failed — turn stands without cards")
    return manifest


async def _detail_rows(ids: list[str]) -> list[dict]:
    """Card facts for a turn that answered by id rather than by search.

    `get_property` / `benchmark_price` / `reauction_history` reach the graph
    by id and put nothing in the sink, so the ids the answer names ARE the
    cards. Reuses the same by-id fetch `artifacts._fallback_artifacts` does.
    """
    if not ids:
        return []
    import asyncio

    from api.tools import cypher_tools as cypher_T

    rows = await asyncio.to_thread(cypher_T.get_auctions_by_ids,
                                   ids[:MAX_DETAIL_IDS])
    return list(rows or [])


def to_payload(manifest: TurnManifest | None) -> dict[str, Any] | None:
    """The manifest as it goes over the wire, or None if there isn't one."""
    return manifest.to_dict() if manifest is not None else None


def history_from_messages(messages: list) -> list[dict[str, Any]]:
    """The thread's questions and answers, ordinal-tagged, in order.

    The server's own count of the conversation. #404 bans joining manifests to
    the client's saved copy: that copy can be edited, truncated or stale, and
    an ordinal computed from it silently attaches turn 4's cards to turn 3.

    Two message kinds are dropped on purpose:

    - **Non-final AI messages** are the tool-calling steps of a turn, not
      answers. Counting them runs the ordinal ahead of the conversation.
    - **Human messages with no `USER_TEXT_DELIMITER`** are not the user.
      `loop.compose_input` always joins at least the date line onto a real
      question, so the only undelimited human messages in a transcript are the
      repair notes `AnswerGate` injects. Returning one would show the user a
      message they never sent.
    """
    from api.agent3.skills import USER_TEXT_DELIMITER

    turn = 0
    out: list[dict[str, Any]] = []
    for m in messages or []:
        kind = getattr(m, "type", "")
        if kind == "human":
            text = message_text(m)
            if USER_TEXT_DELIMITER not in text:
                continue
            out.append({"role": "user",
                        "text": text.rsplit(USER_TEXT_DELIMITER, 1)[-1]})
        elif kind == "ai" and not getattr(m, "tool_calls", None):
            turn += 1
            out.append({"role": "assistant", "text": message_text(m),
                        "turn_index": turn})
    return out
