"""
evals/evaluators.py
-------------------
Custom `pydantic-evals` evaluators for the chat agent, plus the LLM-as-judge
rubric used for answer-quality scoring.

Three gating assertions (pass/fail) and one reported score:

- ``ToolTrajectory`` (assertion) — did the agent call at least one of the
  intent's `acceptable_tools`? The hard CI gate for tool-routing cases,
  mirroring the old golden-question test.
- ``GracefulRefusal`` (assertion) — for out-of-scope cases (Rule 4), did the
  answer decline gracefully (contain an expected decline/pointer phrase)
  instead of fabricating data or promising an action no tool performs? The CI
  gate for refusal cases; a no-op pass on every other case.
- ``NoWriteError`` (assertion) — did the final answer avoid leaking an internal
  read-only/write-rejection error to the user?
- ``CitesAuctionIds`` (assertion) — on listing-style cases, did the answer cite
  at least one auction_id the turn's tools surfaced (role rule 1)? This is the
  behavior the UI matches-panel sync depends on (api/chat/panel.py extracts
  cited ids from the answer text), so an uncited listing answer means the
  panel silently stops following the conversation. Reported per case; its
  aggregate pass rate is gated separately in the runner (env-tunable, report-
  only by default while it burns in).
- ``answer_quality`` (score, via :class:`LLMJudge`) — a 0–1 groundedness/quality
  score from a judge model. Reported and tracked, but *not* a CI gate by
  default, since it is softer and judge-model-dependent.

Evaluators read ``ctx.output`` duck-typed (``.answer`` / ``.tools_called``) to
avoid importing the task-output type and creating an import cycle with
`evals/dataset.py`.
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge

# Assertion names as they appear in the report — referenced by the runner's
# CI gate, so keep them in sync with the class names below.
TOOL_TRAJECTORY = "ToolTrajectory"
GRACEFUL_REFUSAL = "GracefulRefusal"
NO_WRITE_ERROR = "NoWriteError"
CITES_AUCTION_IDS = "CitesAuctionIds"
ANSWER_QUALITY = "answer_quality"

# Substrings that mean an internal read-only guardrail error leaked into the
# user-facing answer (see api/tools/cypher_tools.py write-rejection messages).
_WRITE_ERROR_MARKERS = (
    "run_cypher rejects",
    "rejects writes",
    "rejects write procedures",
)


@dataclass
class ToolTrajectory(Evaluator):
    """Pass if the agent invoked at least one acceptable tool for the case.

    `acceptable_tools` is carried on the case metadata. An empty/missing list
    means "no tool requirement" and always passes (none of the current cases
    do this, but it keeps the evaluator robust to future free-text cases).
    """

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        acceptable = (ctx.metadata or {}).get("acceptable_tools") or []
        if not acceptable:
            return True
        called = getattr(ctx.output, "tools_called", []) or []
        return any(t in acceptable for t in called)


@dataclass
class GracefulRefusal(Evaluator):
    """Gate out-of-scope (Rule 4) cases: the agent must decline gracefully.

    A no-op pass on any case not flagged `expect_refusal` — so it can be
    attached at the dataset level and only bites on refusal cases. For a
    refusal case, passes when the answer contains at least one of the case's
    `refusal_required_any` substrings (case-insensitive): either a pointer the
    agent is required to give (e.g. "save" → the Save button for track/alert
    requests) or a decline phrase for "we don't hold that data" requests
    (litigation, market valuation, credit history). A fabricated answer that
    never declines lacks all of them and fails.

    This is deliberately a deterministic keyword check rather than an LLM judge:
    like the trajectory gate it must be stable enough to block CI, and the
    softer groundedness signal is already covered by `answer_quality`.
    """

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        meta = ctx.metadata or {}
        if not meta.get("expect_refusal"):
            return True
        required = meta.get("refusal_required_any") or []
        if not required:
            # Flagged as a refusal case but given no acceptance lexicon — treat
            # as misconfigured rather than silently passing everything.
            return False
        answer = (getattr(ctx.output, "answer", "") or "").lower()
        return any(marker.lower() in answer for marker in required)


@dataclass
class CitesAuctionIds(Evaluator):
    """On listing-style cases, the answer must cite a surfaced auction_id.

    The runner computes `surfaced_auction_ids` (every id the turn's tool
    results returned) and `cited_auction_ids` (ids from that set appearing in
    the answer text) using the REAL panel extractor (`api.chat.panel`), so
    this asserts exactly what production's matches-panel sync would see.

    Pass when the case doesn't set `expect_citations`, or when the turn
    surfaced no ids at all (a zero-result answer legitimately cites nothing).
    Fail only on the real regression: properties were found but the answer
    named none of them — the panel-starving behavior.
    """

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        if not (ctx.metadata or {}).get("expect_citations"):
            return True
        surfaced = getattr(ctx.output, "surfaced_auction_ids", []) or []
        if not surfaced:
            return True
        cited = getattr(ctx.output, "cited_auction_ids", []) or []
        return bool(cited)


@dataclass
class NoWriteError(Evaluator):
    """Pass unless the final answer leaked an internal write-rejection error."""

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        if not (ctx.metadata or {}).get("must_not_mention_write_error", True):
            return True
        answer = (getattr(ctx.output, "answer", "") or "").lower()
        return not any(marker in answer for marker in _WRITE_ERROR_MARKERS)


# Reference-free quality rubric. The dataset has no fixed "gold answer" (the
# underlying graph changes nightly), so the judge scores groundedness and
# helpfulness given the question — the same reference-free pattern LangSmith's
# LLM-as-judge evaluators use. `include_input=True` feeds the judge the user
# question; the judged "output" is the agent's final answer text.
JUDGE_RUBRIC = """\
You are grading the answer of an assistant for an Indian bank-auction (SARFAESI)
property search platform. The assistant answers strictly from a Neo4j knowledge
graph of Tamil Nadu auctions via tools; it must never invent data.

Give a HIGH score when the answer:
- directly addresses the user's question;
- is internally consistent and grounded (concrete auction_ids / prices /
  counts presented as facts, not vague hedging or invented thresholds);
- clearly states when there are no matching results instead of fabricating;
- is well structured and readable.

Give a LOW score when the answer:
- is off-topic, evasive, or refuses a clearly in-scope question;
- appears to fabricate auction_ids, prices, counts, or property details;
- leaks internal errors, stack traces, tool names, or Cypher to the user;
- contradicts itself.

Score from 0 (poor) to 1 (excellent)."""


def answer_quality_judge(model: object | None = None) -> LLMJudge:
    """Build the answer-quality :class:`LLMJudge`.

    `model` is a pydantic-ai model (instance or known-name string). When None,
    pydantic-evals uses its configured default judge model. Emitted as a 0–1
    *score* named ``answer_quality`` (not an assertion) so a soft quality dip is
    reported without failing CI.
    """
    return LLMJudge(
        rubric=JUDGE_RUBRIC,
        model=model,
        include_input=True,
        score={"evaluation_name": ANSWER_QUALITY, "include_reason": True},
        assertion=False,
    )
