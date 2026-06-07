"""
evals/evaluators.py
-------------------
Custom `pydantic-evals` evaluators for the chat agent, plus the LLM-as-judge
rubric used for answer-quality scoring.

Two gating assertions (pass/fail) and one reported score:

- ``ToolTrajectory`` (assertion) — did the agent call at least one of the
  intent's `acceptable_tools`? This is the hard CI gate, mirroring the old
  golden-question test.
- ``NoWriteError`` (assertion) — did the final answer avoid leaking an internal
  read-only/write-rejection error to the user?
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
NO_WRITE_ERROR = "NoWriteError"
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
