"""
api/chat/v2/schemas.py
----------------------
The typed objects the tiered loop passes around, and the /chat/v2 wire
contract.

Two shapes carry design weight:

**`Synthesis.answer` is the FIRST field.** Structured output arrives as one
JSON blob, which would turn today's token-by-token answer into a silent wait
— a visible product regression shipped alongside the latency win. Because
`answer` is declared first, the model emits it first, and the loop can stream
the characters inside that one string value as they arrive. Reordering these
fields breaks streaming.

**`Synthesis.need_more` is a field, not a prefix.** The spike signalled a
follow-up round by starting its reply with `NEED_MORE:` and had to keep
hardening against the marker leaking into the user's answer. As a typed field
that failure mode does not exist.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ── what the planner emits ──────────────────────────────────────────────────

class PlannedCall(BaseModel):
    tool: str = Field(description="One of the tool names in the catalogue.")
    args: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Arguments for the call. For search_auctions emit only what "
            "CHANGES from the active scope: a new value overrides, an "
            "explicit null drops that filter."
        ),
    )


class Plan(BaseModel):
    """The planner is also the router — there is no separate classification
    call. Exactly one of `calls`, `cypher_request` or `direct_answer` is
    meaningful per plan."""

    calls: list[PlannedCall] = Field(
        default_factory=list,
        description="Every tool call the question needs. They run in parallel, "
                    "so emit all of them now rather than one at a time.",
    )
    scope: Literal["carry", "reset"] = Field(
        default="carry",
        description=(
            "carry = this question narrows the previous one. reset = it "
            "changes the subject, so earlier filters must be dropped. "
            "Getting this wrong silently answers about the wrong city."
        ),
    )
    cypher_request: str | None = Field(
        default=None,
        description="One plain-English line stating what to compute, when no "
                    "tool in the catalogue can express the question.",
    )
    direct_answer: str | None = Field(
        default=None,
        description=(
            "The FINAL ANSWER TEXT the user will read, addressed to them. "
            "Use it for greetings, and for saying plainly that something is "
            "out of scope. NEVER describe what you intend to do — 'I'll "
            "search for...' is a plan, and a plan belongs in `calls`. If a "
            "tool could answer the question, leave this null and emit the "
            "call."
        ),
    )


class CypherSpec(BaseModel):
    cypher: str
    params: dict[str, Any] = Field(default_factory=dict)
    description: str = Field(
        default="", description="One-sentence intent summary shown in the UI chip."
    )


# ── what the synthesizer emits ──────────────────────────────────────────────

class Badge(BaseModel):
    text: str
    tone: Literal["good", "neutral", "warn"] = "neutral"


class Pick(BaseModel):
    auction_id: str
    rank: int
    reason: str = Field(
        description="One line on why THIS property, in the user's terms. Not a "
                    "restatement of its fields."
    )
    badges: list[Badge] = Field(default_factory=list)


class Recommendation(BaseModel):
    """The typed object behind reason-first cards.

    Typed rather than markdown because prose has to be parsed to render and
    cannot be checked: AnswerGate verifies every number here against the
    turn's tool results before a card paints.
    """

    summary: str = Field(default="", description="One line over the whole set.")
    ranked_by: str = Field(
        default="",
        description="The axis the ranking used. Silent ranking is the fastest "
                    "way to lose trust in a recommender.",
    )
    scope_line: str = Field(
        default="",
        description="The scope this answer covers, e.g. 'In Chennai, under "
                    "Rs 40L'. Makes a wrong carried filter visible instead of "
                    "silent.",
    )
    picks: list[Pick] = Field(default_factory=list)


class Synthesis(BaseModel):
    # FIRST — see the module docstring. Streaming depends on this ordering.
    answer: str = Field(description="The final answer, in markdown.")
    recommendation: Recommendation | None = Field(
        default=None,
        description="Populate when the answer surfaces specific properties.",
    )
    need_more: Plan | None = Field(
        default=None,
        description="Only when the results are genuinely insufficient AND one "
                    "more round of specific calls would fix it. A zero-result "
                    "with refine diagnostics is an answer, not a gap.",
    )


# ── the wire contract ───────────────────────────────────────────────────────

class ScopeIn(BaseModel):
    """Conversation state, echoed by the client. Server-authored but
    client-supplied, so `api/chat/v2/scope.py::sanitize_scope` re-validates
    every field on every request."""

    filters: dict[str, Any] = Field(default_factory=dict)
    last_total_count: int | None = None
    last_ids: list[str] = Field(default_factory=list)
    #: The previous question, verbatim and capped. One turn, not a transcript
    #: — enough for a pronoun to have an antecedent.
    last_question: str = ""
    #: {dimension: [label, ...]} the previous turn NAMED, e.g.
    #: {"area": ["Ambattur", "Padappai", ...]}. Filters say what was searched;
    #: these say what the user actually read, which is what "these areas"
    #: refers to. Never merged into tool kwargs — see scope.py.
    last_entities: dict[str, list[str]] = Field(default_factory=dict)
    turn: int = 0


class PanelIn(BaseModel):
    matches: list[str] = Field(default_factory=list)
    pinned: list[str] = Field(default_factory=list)
    dismissed: list[str] = Field(default_factory=list)


class ChatV2Request(BaseModel):
    message: str
    # Replaces v1's `message_history` outright: one small dict instead of a
    # transcript that grows every turn and is re-billed on each one.
    scope: ScopeIn | None = None
    panel: PanelIn | None = None
    active_filters: dict[str, Any] | None = None
    mode: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None


class ExecutedCallOut(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    ms: int = 0
    tier: int = 1
    error: str | None = None


class GateVerdictOut(BaseModel):
    """The answer gate's finding for this turn.

    Returned rather than only logged because the fire rate is the measurement
    that decides whether enforcement is affordable, and a number nobody can
    see does not get acted on. Report-only: a flagged answer still ships.
    """

    ok: bool = True
    unsupported_ids: list[str] = Field(default_factory=list)
    unsupported_amounts: list[str] = Field(default_factory=list)
    reason: str = ""


class ChatV2Response(BaseModel):
    answer: str
    recommendation: Recommendation | None = None
    scope: ScopeIn
    plan: list[ExecutedCallOut] = Field(default_factory=list)
    # Deliberately the same shape v1 returns, so the whole panel path in
    # web/app.js works unchanged and the frontend diff for a flag flip stays
    # to the conversation-state channel.
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    #: Populated for admins only — it is a diagnostic, not product surface.
    gate: GateVerdictOut | None = None
