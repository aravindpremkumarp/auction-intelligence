"""
api/agent3/gates.py
-------------------
Two custom middlewares that enforce in code what a prompt can only request.

`instructions.md` already says "ground every number" and "no sold prices".
An instruction is a preference the model weighs against everything else in
its context; a gate is a property of the system. These exist for the rules
where being wrong is worse than being unhelpful.

**`IntentGate` (before_agent)** — refuses bulk personal-data harvesting
before a single token is spent. See its docstring for where the line sits and
why it is not the only barrier.

**`AnswerGate` (after_model)** — checks the draft answer against what the
tools actually returned, and spends at most one repair call.

---

**What the gate can and cannot check, honestly.** The question "why gate at
all, when the data comes from Neo4j and Neo4j cannot hallucinate" has a
precise answer: the graph constrains *retrieval*, not the prose written on
the way out. Between the tool result and the answer the model still
paraphrases, transcribes and does arithmetic, and each of those is a place a
number can go wrong without the graph being consulted again.

So the classes here are split by whether a violation is *definitionally*
wrong or merely *suspicious*, and only the first kind triggers a repair:

| Class | Repair? | Why |
|---|---|---|
| Unknown `auction_id` | **yes** | No arithmetic produces an id. If it is not in the tool output it was invented or mis-transcribed, full stop. |
| A sale price / market valuation | **yes** | `Auction.outcome` is only ever "unsold". Any figure attached to a sale verb is describing something this graph does not contain. |
| Unhedged lot fact from a multi-lot notice | **yes** | The scope rule. The tool tagged it `notice`; dropping the tag in prose is the confidently-wrong failure the whole design is built around. |
| ₹ amounts, sqft, counts | **no — recorded only** | The model legitimately derives these: a difference between two reserves, a per-sqft, a rounded mean. A strict check flags correct arithmetic, and a gate that cries wolf gets turned off. |

The fourth row is deliberately advisory, and the numbers it reports are the
evidence for whether it could ever be promoted. Promoting it on a hunch would
trade a rare wrong number for a common wrong refusal.
"""
from __future__ import annotations

import re
from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from api.agent3.skills import USER_TEXT_DELIMITER

# ── shared extraction ────────────────────────────────────────────────────

#: A portal `auction_id` is exactly six digits — verified across all 2,964
#: listings (658842–842929, `size(auction_id)` 6 for every one). The band
#: below is deliberately wider than the observed range, because ids are a
#: portal sequence that grows as new listings are scraped; the check does not
#: want to start flagging real ids the day the range moves.
#:
#: The lookarounds reject a six-digit run that is part of a longer number:
#: a bare digit either side, or a comma/period that is itself between digits
#: (`1,234,567`, `1234.567890`). They must NOT reject a trailing sentence
#: period — the first draft used `(?![\d,.])` and silently matched nothing at
#: the end of a sentence, which is where an id in prose almost always sits.
#: Caught by this file's first test on its first run.
ID_LIKE = re.compile(r"(?<!\d)(?<!\d,)(?<!\d\.)(\d{6})(?!\d)(?!,\d)(?!\.\d)")
ID_BAND = (600_000, 999_999)

#: Currency context around a number, checked so a six-digit *price* is not
#: mistaken for an id. `₹6,50,000` normalises to 650000, which is inside the
#: id band; without this the gate would flag a correctly-quoted reserve.
_CURRENCY_BEFORE = re.compile(r"(₹|rs\.?|inr)\s*$", re.I)
_CURRENCY_AFTER = re.compile(r"^\s*(lakh|lakhs|lac|crore|crores|cr\b|l\b|rupees)", re.I)

#: A sale verb carrying a figure. Each of these asserts something
#: `Auction.outcome` cannot support. The figure requirement is what keeps
#: "this graph has no sold prices" and "I cannot tell you what it sold for"
#: from tripping the gate — those are the sentences we WANT.
_SALE_CLAIM = re.compile(
    r"\b(sold\s+(?:for|at)|went\s+for|fetched|hammer\s+price|winning\s+bid|"
    r"final\s+bid|sale\s+price\s+(?:was|is)|market\s+value\s+(?:is|was|of)|"
    r"valued\s+at|appraised\s+at)\b[^.\n]{0,60}?"
    r"(₹|rs\.?|inr|\d)", re.I)

#: Phrases that carry the scope caveat. Any one of them in the prose means
#: the model did not silently present a notice-level value as this property's
#: own. Kept broad on purpose: the gate is asking "did it hedge at all", not
#: grading the wording.
_SCOPE_HEDGE = re.compile(
    r"(covers?\s+\d+\s+lots?|\d+\s+lots?\b|multi-?lot|across\s+the\s+lots|"
    r"which\s+lot|not\s+necessarily\s+this|describes?\s+the\s+notice|"
    r"notice[- ]level|notice\s+as\s+a\s+whole|combined\s+notice|"
    r"range\s+across|for\s+the\s+notice\b|scope)", re.I)

#: Words that mean the prose is actually stating an extent. Without one of
#: these the scope check has nothing to be wrong about, so it stays quiet.
_EXTENT_CLAIM = re.compile(
    r"(\bsq\.?\s?ft\b|square\s+feet|\bsqft\b|\bacres?\b|\bcents?\b|"
    r"\bgrounds?\b|\bhectares?\b|extent\s+of|measur\w+\s+\d)", re.I)


def _text_of(message: Any) -> str:
    """A message's text, whether the provider sent a string or content blocks."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content
                        if isinstance(part, dict))
    return str(content or "")


def tool_output_text(messages: list) -> str:
    """Every tool result in the thread, concatenated.

    The whole thread rather than the current turn, deliberately: on a
    follow-up ("tell me more about the second one") the model cites ids that
    a *previous* turn's search returned, and scoping this to the current turn
    would flag every one of them.
    """
    return "\n".join(_text_of(m) for m in messages
                     if getattr(m, "type", "") == "tool")


def ungrounded_ids(answer: str, evidence: str) -> list[str]:
    """Six-digit ids in the prose that appear nowhere in any tool result.

    The currency guard is what makes this safe to hard-gate: a number written
    as a price is skipped entirely rather than being checked as an id.
    """
    out: list[str] = []
    for m in ID_LIKE.finditer(answer or ""):
        token = m.group(1)
        if not (ID_BAND[0] <= int(token) <= ID_BAND[1]):
            continue
        if _CURRENCY_BEFORE.search(answer[max(0, m.start() - 6):m.start()]):
            continue
        if _CURRENCY_AFTER.match(answer[m.end():m.end() + 12]):
            continue
        if token in evidence:
            continue
        if token not in out:
            out.append(token)
    return out


def sale_claims(answer: str) -> list[str]:
    """Assertions that a property sold, or is worth, some amount."""
    return [m.group(0).strip() for m in _SALE_CLAIM.finditer(answer or "")]


#: `notice_lot_count` / `lot_count` as they appear in a serialised tool
#: payload. Both quote styles, because a ToolMessage's content is whatever
#: the tool node's serialiser produced — `json.dumps` gives double quotes,
#: `str(dict)` gives single, and which one arrives is not ours to fix.
_LOT_COUNT = re.compile(r"""["'](?:notice_)?lot_count["']\s*:\s*(\d+)""")


def lot_counts(messages: list) -> list[int]:
    """Every `notice_lot_count` the model was shown in this thread.

    Read off the tool text rather than re-queried: the gate must judge what
    the model actually saw, not what the graph says now.
    """
    return [int(c) for c in _LOT_COUNT.findall(tool_output_text(messages))]


def scope_violation(answer: str, messages: list) -> str | None:
    """An extent stated with no hedge, when every notice in view is multi-lot.

    Three conditions, all required, because each one alone produces noise:

    1. the prose actually states an extent (otherwise there is no claim);
    2. **every** `notice_lot_count` the model saw is >1 (if any single-lot
       notice is in view, a bare extent may correctly be that one's);
    3. no hedging phrase appears anywhere in the answer.

    Condition 2 is the conservative half. It makes this a check on the
    one-property case — `get_property` on a multi-lot notice, which is
    exactly where the scope rule bites — and lets broad searches through,
    where rows carry their own per-row tags.
    """
    if not _EXTENT_CLAIM.search(answer or ""):
        return None
    counts = lot_counts(messages)
    if not counts or any(c <= 1 for c in counts):
        return None
    if _SCOPE_HEDGE.search(answer or ""):
        return None
    seen = ", ".join(str(c) for c in sorted(set(counts)))
    return (f"every notice in view covers several lots ({seen}), and the "
            f"answer states an extent without saying so")


#: Numeric classes recorded but never repaired — see the module docstring.
_RUPEE = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)\s*"
                    r"(lakh|lakhs|lac|crore|crores|cr|l)?\b", re.I)
_MULTIPLIER = {"lakh": 1e5, "lakhs": 1e5, "lac": 1e5,
               "crore": 1e7, "crores": 1e7, "cr": 1e7, "l": 1e5}


def _numbers_in(text: str) -> set[float]:
    return {float(t.replace(",", ""))
            for t in re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")}


def unverified_amounts(answer: str, evidence: str) -> list[str]:
    """₹ figures with no matching number in tool output, at any rounding.

    **Advisory only.** A miss here is at least as likely to be legitimate
    arithmetic — a price difference, a per-sqft, a rounded mean — as an
    invention. Matching is done at every significant-figure width so that
    "about ₹2 crore" drawn from a mean of 19,987,432 counts as grounded,
    which is the single most common shape in the smoke answers.
    """
    pool = _numbers_in(evidence)
    rounded: set[float] = set()
    for value in pool:
        for sig in range(1, 5):
            try:
                rounded.add(float(f"%.{sig}g" % value))
            except (ValueError, OverflowError):  # pragma: no cover - defensive
                continue
    misses: list[str] = []
    for m in _RUPEE.finditer(answer or ""):
        raw, unit = m.group(1), (m.group(2) or "").lower()
        try:
            value = float(raw.replace(",", ""))
        except ValueError:  # pragma: no cover - the regex guarantees digits
            continue
        value *= _MULTIPLIER.get(unit, 1.0)
        if value in pool or value in rounded:
            continue
        if any(abs(value - p) <= max(1.0, abs(p) * 0.005) for p in pool):
            continue
        misses.append(m.group(0).strip())
    return misses


# ── the gates ────────────────────────────────────────────────────────────

class GateState(AgentState):
    """One extra field: how many repairs this run has already spent."""

    answer_gate_repairs: NotRequired[int]


#: One, not two. A repair is a full model call, so each one is a turn's
#: worth of latency and cost spent on an answer the user is already waiting
#: for. If a model that has just been told exactly which id it invented
#: invents another, the problem is upstream of the gate — more attempts would
#: buy a longer wait for the same defect. Raise this only against a measured
#: rate of second-attempt successes; there is no such measurement yet.
MAX_REPAIRS = 1


class AnswerGate(AgentMiddleware):
    """Check the draft against the tool results; repair once, then degrade.

    Runs only on a *final* draft — an AI message with no tool calls. A
    message that still has tool calls is mid-thought, and the numbers in it
    are not an answer to anyone.

    On a violation the rejected draft is **removed** rather than left in the
    transcript. It is checkpointed history otherwise: re-sent and re-billed
    on every later turn, and readable by a later turn's model as if it were
    something we had said. The correction goes in as a human-turn note, and
    the model writes the answer again with the tool results still in view.
    """

    state_schema = GateState

    def __init__(self, *, repair: bool = True, max_repairs: int = MAX_REPAIRS):
        super().__init__()
        self.repair = repair
        self.max_repairs = max_repairs

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: dict, runtime: Any = None) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        if not messages:
            return None
        draft = messages[-1]
        if not isinstance(draft, AIMessage) or draft.tool_calls:
            return None

        answer = _text_of(draft)
        evidence = tool_output_text(messages)
        problems = self.inspect(answer, messages, evidence)
        if not problems["blocking"]:
            return None

        spent = state.get("answer_gate_repairs", 0) or 0
        if not self.repair or spent >= self.max_repairs:
            # Degrade rather than loop. The answer stands; the failure is on
            # the record for the eval to score. Silently accepting AND
            # silently retrying forever are both worse.
            return None

        note = _repair_note(problems["blocking"])
        # `id` is assigned by the provider adapter, so in practice it is
        # always set — but a message with no id cannot be removed, and
        # crashing the turn over housekeeping would be worse than leaving one
        # rejected draft in the transcript.
        updates: list[Any] = []
        if getattr(draft, "id", None):
            updates.append(RemoveMessage(id=draft.id))
        updates.append(HumanMessage(content=note))
        return {"jump_to": "model", "answer_gate_repairs": spent + 1,
                "messages": updates}

    async def aafter_model(self, state: dict, runtime: Any = None) -> dict[str, Any] | None:
        return self.after_model(state, runtime)

    def inspect(self, answer: str, messages: list, evidence: str | None = None) -> dict:
        """The findings, split into what repairs and what is only recorded.

        Exposed separately from the hook so the eval harness can score a
        finished answer without running a graph.
        """
        evidence = tool_output_text(messages) if evidence is None else evidence
        blocking: list[str] = []

        bad_ids = ungrounded_ids(answer, evidence)
        if bad_ids:
            blocking.append(
                f"cites auction_id(s) {', '.join(bad_ids)}, which appear in no "
                f"tool result in this conversation")

        for claim in sale_claims(answer):
            blocking.append(
                f"states a sale or valuation ({claim!r}); this graph has no "
                f"sold prices and no valuations")

        scope = scope_violation(answer, messages)
        if scope:
            blocking.append(f"scope: {scope}")

        return {"blocking": blocking,
                "advisory": [f"unverified amount {a!r}"
                             for a in unverified_amounts(answer, evidence)]}


def _repair_note(problems: list[str]) -> str:
    """The correction handed back to the model.

    Names the specific defect and re-states the rule, rather than saying "try
    again": a bare retry on a temperature-0 model reproduces the same draft.
    """
    listed = "\n".join(f"- {p}" for p in problems)
    return (
        "Your draft answer was not sent. It broke a rule that is checked in "
        "code, not left to judgement:\n"
        f"{listed}\n\n"
        "Write the answer again using only what the tool results above "
        "actually contain. If a fact is not in them, say it is not available "
        "rather than supplying it. Do not call any more tools.")


#: Person-nouns in this domain. `Borrower` nodes carry real names of real
#: people who defaulted on a loan.
_PEOPLE = (r"borrowers?|defaulters?|debtors?|owners?|guarantors?|mortgagors?|"
           r"people|persons?|individuals?|customers?|clients?")

#: Bulk enumeration: an "every/all/list" quantifier over a person-noun. The
#: quantifier is what separates this from "who is the borrower on 748779",
#: which is one published legal notice and stays allowed.
_BULK_PEOPLE = re.compile(
    rf"\b(all|every|each|list(?:\s+of)?|names?\s+of|full\s+list|"
    rf"complete\s+list|database|export|dump|spreadsheet|csv)\b"
    rf"[^.?!\n]{{0,40}}\b({_PEOPLE})\b", re.I)

#: The same request phrased the other way round ("borrowers in Coimbatore,
#: all of them" / "every one of the defaulters").
_BULK_PEOPLE_REVERSED = re.compile(
    rf"\b({_PEOPLE})\b[^.?!\n]{{0,40}}\b(all|every|each|"
    rf"full\s+list|complete\s+list|entire\s+list)\b", re.I)

#: Contact details are a separate ask, and a worse one — the notice publishes
#: a name against a property, not a way to reach the person at home.
_CONTACT_HARVEST = re.compile(
    rf"\b(phone|mobile|contact|email|e-mail|address(?:es)?|"
    rf"whatsapp|number)s?\b[^.?!\n]{{0,40}}\b({_PEOPLE})\b|"
    rf"\b({_PEOPLE})\b[^.?!\n]{{0,40}}\b(phone|mobile|contact\s+details?|"
    rf"email|e-mail|home\s+address(?:es)?|whatsapp)\b", re.I)

REFUSAL = (
    "I can't put together a list of borrowers or their contact details. The "
    "names in this data belong to real people who defaulted on a loan, and "
    "collecting them across properties is a different thing from reading one "
    "published sale notice.\n\n"
    "What I can do: look up a single listing in full — including the parties "
    "the notice names — or search properties by city, bank, price, extent, "
    "possession, survey number, or notice text. Ask me about a property and "
    "I'll tell you everything the notice says about it."
)


class IntentGate(AgentMiddleware):
    """Refuse bulk personal-data harvesting before the model runs.

    **This is defence in depth, not the only barrier**, and saying otherwise
    would overstate it. The tool shapes are the primary control: no search
    returns borrower names in its rows, and `get_property` — the only tool
    that returns names at all — is capped at five ids per call, against a
    10-call ceiling per turn. Someone determined to harvest names would have
    to walk the graph five at a time across many turns. This gate closes the
    cheap version: one plainly-phrased request that would otherwise start
    that walk.

    **Why not `PIIMiddleware`.** It is the obvious-looking fit and it is the
    wrong tool twice over. It detects emails, credit cards, IPs, MAC
    addresses and URLs — none of which is what is sensitive here, which is an
    Indian personal name attached to a loan default. And it acts on message
    *content*, redacting or blocking what has already been retrieved, where
    the cost worth avoiding is the retrieval itself.

    **Regex, not a model call.** A classifier tier would cost a round-trip on
    every single turn to catch a case that arrives rarely, and it would fail
    open when the provider is down. A regex tier fails closed and costs
    nothing. It is also beatable by anyone who rephrases, which is why the
    tool shapes above have to carry the real weight.
    """

    state_schema = GateState

    @hook_config(can_jump_to=["end"])
    def before_agent(self, state: dict, runtime: Any = None) -> dict[str, Any] | None:
        question = _latest_human_text(state.get("messages") or [])
        if not question or not is_bulk_personal_request(question):
            return None
        return {"jump_to": "end", "messages": [AIMessage(content=REFUSAL)]}

    async def abefore_agent(self, state: dict, runtime: Any = None) -> dict[str, Any] | None:
        return self.before_agent(state, runtime)


def is_bulk_personal_request(question: str) -> bool:
    """True for "every defaulter in Coimbatore with addresses".

    False for "who is the borrower on 748779" — that is one published legal
    notice, and refusing it would make the agent useless for the diligence it
    exists to do.
    """
    text = question or ""
    if _CONTACT_HARVEST.search(text):
        return True
    return bool(_BULK_PEOPLE.search(text) or _BULK_PEOPLE_REVERSED.search(text))


def _latest_human_text(messages: list) -> str:
    """The user's own words, with any injected skill text stripped off.

    This split is load-bearing, not tidiness. `loop.compose_input` prepends
    the turn's loaded skills to the same human message, and the skill files
    are full of the exact phrases this gate matches — the diligence skill
    talks about parties and borrowers at length. Matching the composed
    message would refuse every turn that loaded that skill, which is the
    worst possible failure mode for a safety gate: it fires on the honest
    questions and teaches everyone to remove it.
    """
    for m in reversed(messages):
        if getattr(m, "type", "") == "human":
            text = _text_of(m)
            return text.rsplit(USER_TEXT_DELIMITER, 1)[-1]
    return ""
