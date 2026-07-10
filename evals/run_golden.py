"""
evals/run_golden.py
--------------------
Live golden-question runner. Executes every catalogue case through the real
chat agent (OpenRouter + Neo4j), applies the evaluators, prints a report, and
exits non-zero when the tool-trajectory pass rate regresses below threshold —
the CI gate run nightly by `.github/workflows/golden.yml`.

When ``LOGFIRE_TOKEN`` is set, the whole run (every agent turn, LLM call, tool
call, and evaluation result) streams to Logfire so an eval is browsable as a
trace and comparable across runs — the LangSmith "experiment" view.

Usage (needs OpenRouter + Neo4j credentials in the environment):

    python -m evals.run_golden

Env knobs:
    EVAL_MIN_TRAJECTORY_PASS  CI gate threshold (default 0.85)
    EVAL_MIN_CITATION_PASS    citation-discipline gate over the listing cases
                              (default 0 = report-only while it burns in)
    EVAL_MAX_CONCURRENCY      parallel agent runs (default 4)
    EVAL_CHAT_MODEL           logical chat model to eval: "flash"/"pro"
                              (default "flash" — the free-tier model is both
                              the cheaper eval and the harder tool-routing
                              bar; an unknown value falls back to "pro" via
                              build_chat_run_overrides)
    EVAL_DISABLE_JUDGE=1      skip the LLM-as-judge quality score
    EVAL_JUDGE_MODEL          judge model id (default: OPENROUTER_MODEL)
"""
from __future__ import annotations

import asyncio
import os
import sys

from evals.dataset import ChatTaskOutput, build_dataset
from evals.evaluators import CITES_AUCTION_IDS, GRACEFUL_REFUSAL, TOOL_TRAJECTORY

MIN_TRAJECTORY_PASS = float(os.getenv("EVAL_MIN_TRAJECTORY_PASS", "0.85"))
MIN_CITATION_PASS = float(os.getenv("EVAL_MIN_CITATION_PASS", "0"))
MAX_CONCURRENCY = int(os.getenv("EVAL_MAX_CONCURRENCY", "4"))
CHAT_MODEL = os.getenv("EVAL_CHAT_MODEL", "flash")


async def _run_agent(question: str) -> ChatTaskOutput:
    """The eval 'task': run one question through the real agent and capture
    the answer, the tools it called, and the auction_ids it surfaced/cited.

    The surfaced/cited sets are computed with the REAL panel extractor
    (api/chat/panel.py — pure, no I/O), so the citation assertion tests
    exactly what production's matches-panel sync would see on this answer.
    """
    from pydantic_ai.messages import ToolReturnPart

    from api.agent import ChatDeps, agent, build_chat_run_overrides
    from api.chat.panel import cited_ids, known_auction_ids

    # Same per-request override path the chat router uses, so the eval runs
    # the resolved EVAL_CHAT_MODEL instead of the agent's paid default.
    result = await agent.run(
        question, deps=ChatDeps(), **build_chat_run_overrides(CHAT_MODEL, None)
    )
    seen: set[str] = set()
    tools: list[str] = []
    tool_returns: list[tuple[str, object]] = []
    for msg in result.all_messages():
        for part in getattr(msg, "parts", []):
            name = getattr(part, "tool_name", None)
            if name and name not in seen:
                seen.add(name)
                tools.append(name)
            if isinstance(part, ToolReturnPart):
                tool_returns.append((part.tool_name, part.content))
    answer = result.output or ""
    surfaced = known_auction_ids(tool_returns)
    return ChatTaskOutput(
        answer=answer,
        tools_called=tools,
        surfaced_auction_ids=sorted(surfaced),
        cited_auction_ids=cited_ids(answer, surfaced),
    )


async def main() -> int:
    # Stream the eval run to Logfire when configured (no-op otherwise).
    from api.telemetry import configure_telemetry

    configure_telemetry()

    include_judge = os.getenv("EVAL_DISABLE_JUDGE") != "1"
    dataset = build_dataset(include_judge=include_judge)
    report = await dataset.evaluate(
        _run_agent,
        name="golden-questions",
        max_concurrency=MAX_CONCURRENCY,
    )
    report.print(include_input=True, include_output=False)

    # CI gate: each case's PRIMARY assertion. Tool-routing cases are gated on
    # ToolTrajectory; out-of-scope refusal cases are gated on GracefulRefusal
    # (their `acceptable_tools` is empty, so ToolTrajectory auto-passes and
    # would inflate the rate — the refusal behavior is the real thing to gate).
    # The LLM-judge quality score is reported in the table above but
    # intentionally not gated here.
    total = len(report.cases)
    passed = 0
    for c in report.cases:
        expect_refusal = bool((getattr(c, "metadata", None) or {}).get("expect_refusal"))
        key = GRACEFUL_REFUSAL if expect_refusal else TOOL_TRAJECTORY
        r = c.assertions.get(key)
        if r is not None and r.value:
            passed += 1
    rate = passed / total if total else 0.0
    print(
        f"\nPrimary-assertion pass rate (trajectory + refusal): "
        f"{passed}/{total} = {rate:.1%} (threshold {MIN_TRAJECTORY_PASS:.0%})"
    )

    # Citation discipline over the listing cases (the behavior the UI matches
    # panel depends on). Report-only until EVAL_MIN_CITATION_PASS is raised
    # above 0 — then it gates like the primary rate.
    cite_cases = [
        c for c in report.cases
        if (getattr(c, "metadata", None) or {}).get("expect_citations")
    ]
    cite_passed = sum(
        1
        for c in cite_cases
        if (r := c.assertions.get(CITES_AUCTION_IDS)) is not None and r.value
    )
    cite_rate = cite_passed / len(cite_cases) if cite_cases else 1.0
    print(
        f"Citation pass rate (listing cases): {cite_passed}/{len(cite_cases)} "
        f"= {cite_rate:.1%} (threshold {MIN_CITATION_PASS:.0%}"
        f"{'' if MIN_CITATION_PASS else ' — report-only'})"
    )

    failed = False
    if rate < MIN_TRAJECTORY_PASS:
        print(
            f"REGRESSION: primary pass rate {rate:.1%} below "
            f"threshold {MIN_TRAJECTORY_PASS:.0%}",
            file=sys.stderr,
        )
        failed = True
    if cite_rate < MIN_CITATION_PASS:
        print(
            f"REGRESSION: citation pass rate {cite_rate:.1%} below "
            f"threshold {MIN_CITATION_PASS:.0%}",
            file=sys.stderr,
        )
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
