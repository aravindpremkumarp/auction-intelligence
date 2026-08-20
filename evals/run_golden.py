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
    EVAL_AGENT                which agent to eval: "v1" (the pydantic-ai
                              ReAct agent, default) or "v2" (the tiered
                              loop). Both are scored by the SAME cases and
                              evaluators — that is what makes the two runs
                              comparable as a migration gate.
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

from evals.dataset import build_dataset
from evals.tasks import agent_id, golden_task
from evals.evaluators import CITES_AUCTION_IDS, GRACEFUL_REFUSAL, TOOL_TRAJECTORY

MIN_TRAJECTORY_PASS = float(os.getenv("EVAL_MIN_TRAJECTORY_PASS", "0.85"))
MIN_CITATION_PASS = float(os.getenv("EVAL_MIN_CITATION_PASS", "0"))
MAX_CONCURRENCY = int(os.getenv("EVAL_MAX_CONCURRENCY", "4"))
CHAT_MODEL = os.getenv("EVAL_CHAT_MODEL", "flash")


def report_cost(cases, label: str = "") -> None:
    """Print token/latency totals for a completed run.

    Cost is REPORTED, never gated — a cheap wrong answer is still wrong. It
    exists because the tiered loop's claim was that it is cheaper as well as
    faster, and until now the eval measured neither.

    Prints nothing when no case reported usage, so an agent binding without
    accounting degrades quietly instead of breaking the runner.
    """
    from evals.tasks import USAGE_KEYS

    totals: dict[str, float] = {}
    counted = 0
    for c in cases:
        usage = getattr(getattr(c, "output", None), "usage", None)
        if not isinstance(usage, dict) or not usage:
            continue
        counted += 1
        for k in USAGE_KEYS:
            v = usage.get(k)
            if isinstance(v, (int, float)):
                totals[k] = totals.get(k, 0) + v
    if not counted:
        print("\nCost: not reported by this agent.")
        return

    n = counted
    inp = totals.get("input_tokens", 0)
    cached = totals.get("cached_tokens", 0)
    print(f"\nCost over {n} case(s){label}:")
    print(f"  model calls   {totals.get('llm_calls', 0):>12,.0f}  "
          f"({totals.get('llm_calls', 0) / n:.2f} per case)")
    print(f"  input tokens  {inp:>12,.0f}  ({inp / n:,.0f} per case)")
    print(f"  cached input  {cached:>12,.0f}  "
          # The number that says whether a stable prompt prefix is actually
          # being billed at the cache rate. A steady 0 means it is not.
          f"({(cached / inp * 100) if inp else 0:.1f}% of input)")
    print(f"  output tokens {totals.get('output_tokens', 0):>12,.0f}  "
          f"({totals.get('output_tokens', 0) / n:,.0f} per case)")
    if totals.get("seconds"):
        print(f"  agent seconds {totals['seconds']:>12,.1f}  "
              f"({totals['seconds'] / n:.1f} per case)")



def with_progress(task, total: int):
    """Wrap the eval task so each completed case prints a line to stderr.

    The runner used to print nothing until every case finished, so a
    48-minute run was indistinguishable from a hung one. Wrapping the task
    rather than hooking the library keeps this working across pydantic-evals
    versions, and it counts *completions* — which, at concurrency 4, is the
    number you actually want.

    stderr on purpose: the results table on stdout stays parseable.
    """
    import functools
    import time

    done = {"n": 0}

    @functools.wraps(task)
    async def wrapped(question: str):
        started = time.perf_counter()
        try:
            return await task(question)
        finally:
            done["n"] += 1
            print(f"[{done['n']:>3}/{total}] {time.perf_counter() - started:5.1f}s  "
                  f"{question[:64]}", file=sys.stderr, flush=True)

    return wrapped


async def main() -> int:
    # Stream the eval run to Logfire when configured (no-op otherwise).
    from api.telemetry import configure_telemetry

    configure_telemetry()

    include_judge = os.getenv("EVAL_DISABLE_JUDGE") != "1"
    dataset = build_dataset(include_judge=include_judge)
    print(f"agent under test: {agent_id()}")
    report = await dataset.evaluate(
        with_progress(golden_task(), len(dataset.cases)),
        name=f"golden-questions-{agent_id()}",
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

    report_cost(report.cases)

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
