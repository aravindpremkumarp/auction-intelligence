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
from evals.evaluators import TOOL_TRAJECTORY

MIN_TRAJECTORY_PASS = float(os.getenv("EVAL_MIN_TRAJECTORY_PASS", "0.85"))
MAX_CONCURRENCY = int(os.getenv("EVAL_MAX_CONCURRENCY", "4"))
CHAT_MODEL = os.getenv("EVAL_CHAT_MODEL", "flash")


async def _run_agent(question: str) -> ChatTaskOutput:
    """The eval 'task': run one question through the real agent and capture
    the answer + the tools it called along the way."""
    from api.agent import ChatDeps, agent, build_chat_run_overrides

    # Same per-request override path the chat router uses, so the eval runs
    # the resolved EVAL_CHAT_MODEL instead of the agent's paid default.
    result = await agent.run(
        question, deps=ChatDeps(), **build_chat_run_overrides(CHAT_MODEL, None)
    )
    seen: set[str] = set()
    tools: list[str] = []
    for msg in result.all_messages():
        for part in getattr(msg, "parts", []):
            name = getattr(part, "tool_name", None)
            if name and name not in seen:
                seen.add(name)
                tools.append(name)
    return ChatTaskOutput(answer=result.output or "", tools_called=tools)


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

    # CI gate: the tool-trajectory assertion pass rate. The LLM-judge quality
    # score is reported in the table above but intentionally not gated here.
    total = len(report.cases)
    passed = sum(
        1
        for c in report.cases
        if (r := c.assertions.get(TOOL_TRAJECTORY)) is not None and r.value
    )
    rate = passed / total if total else 0.0
    print(
        f"\nTool-trajectory pass rate: {passed}/{total} = {rate:.1%} "
        f"(threshold {MIN_TRAJECTORY_PASS:.0%})"
    )
    if rate < MIN_TRAJECTORY_PASS:
        print(
            f"REGRESSION: trajectory pass rate {rate:.1%} below "
            f"threshold {MIN_TRAJECTORY_PASS:.0%}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
