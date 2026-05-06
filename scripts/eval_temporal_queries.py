"""
eval_temporal_queries.py
------------------------
Drives 15 representative time-based questions through the chat agent's
/chat endpoint and prints a pass/fail table with the tool the agent
picked and the row count it returned.

Designed to be re-run after prompt or tool changes to measure delta.

Usage:
    # Anonymous chat is rate-limited to 10/hour. Disable for a clean run:
    RATELIMIT_DISABLED=1 uvicorn api.main:app --reload

    # Then in another shell:
    python scripts/eval_temporal_queries.py --tag baseline
    python scripts/eval_temporal_queries.py --tag post-fix --base-url http://localhost:8000

A "pass" means the agent answered AND the tool returned a non-empty
result. A "partial" means it answered but used run_cypher when a
structured tool would have been better, or returned suspiciously few
rows. A "fail" means empty answer or empty tool result.

Writes scripts/_eval_temporal_<tag>.json so two runs can be diffed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx


# Each question has:
#   id              — short stable identifier (used for diffing across runs)
#   prompt          — what we send to /chat
#   expect_nonempty — should at least one tool call return rows?
#   prefer_tool     — name of the structured tool that would be ideal;
#                     if set and the agent picks run_cypher instead, the
#                     verdict drops to PARTIAL even if the answer is non-empty.
#                     None means run_cypher is fine.
#   note            — human context, printed under the verdict.
QUESTIONS: list[dict[str, Any]] = [
    {
        "id": "Q01_ending_next_7d",
        "prompt": "How many auctions are ending in the next 7 days?",
        "expect_nonempty": True,
        "prefer_tool": None,
        "note": "Tests auction_end_dt range with datetime() + duration({days: 7}).",
    },
    {
        "id": "Q02_between_dates",
        "prompt": "Show me auctions starting between May 15 2026 and June 1 2026.",
        "expect_nonempty": True,
        "prefer_tool": "search_auctions",
        "note": "search_auctions accepts both bounds via starts_after/starts_before.",
    },
    {
        "id": "Q03_deadline_gap",
        "prompt": "For each auction, what's the gap in hours between the application deadline and the auction start? Give me the distribution.",
        "expect_nonempty": True,
        "prefer_tool": None,
        "note": "Tests duration.between() / duration.inSeconds().",
    },
    {
        "id": "Q04_per_month",
        "prompt": "How many auctions per month do we have in the dataset?",
        "expect_nonempty": True,
        "prefer_tool": None,
        "note": "Diagnostic: prompt cheat-sheet shows substring() which fails on DATETIME.",
    },
    {
        "id": "Q05_weekday",
        "prompt": "Which weekday do most auctions happen on?",
        "expect_nonempty": True,
        "prefer_tool": None,
        "note": "Tests .dayOfWeek component accessor.",
    },
    {
        "id": "Q06_business_hours",
        "prompt": "How many auctions start during business hours (9 AM to 5 PM)?",
        "expect_nonempty": True,
        "prefer_tool": None,
        "note": "Tests .hour component accessor.",
    },
    {
        "id": "Q07_q2_2026",
        "prompt": "How many auctions are scheduled in Q2 2026 (April through June)?",
        "expect_nonempty": True,
        "prefer_tool": "search_auctions",
        "note": "Agent computes April-June bounds and calls search_auctions.",
    },
    {
        "id": "Q08_last_30d",
        "prompt": "How many auctions happened in the last 30 days?",
        "expect_nonempty": True,
        "prefer_tool": "search_auctions",
        "note": "search_auctions(include_past=True) covers this.",
    },
    {
        "id": "Q09_deadline_within_24h",
        "prompt": "How many auctions have an application deadline within 24 hours of the auction start?",
        "expect_nonempty": True,
        "prefer_tool": None,
        "note": "Tests duration arithmetic.",
    },
    {
        "id": "Q10_reauction_velocity",
        "prompt": "What's the average number of days between a failed auction and its re-list, across all re-auction chains?",
        "expect_nonempty": True,
        "prefer_tool": None,
        "note": "Walks :SAME_PROPERTY_AS chains with duration.inDays().",
    },
    {
        "id": "Q11_next_50_by_date",
        "prompt": "Show me the next 50 auctions ordered by start date.",
        "expect_nonempty": True,
        "prefer_tool": "search_auctions",
        "note": "search_auctions(order_by='deadline_asc', limit=50).",
    },
    {
        "id": "Q12_earliest_this_week",
        "prompt": "What's the earliest auction starting this week?",
        "expect_nonempty": True,
        "prefer_tool": "search_auctions",
        "note": "search_auctions with this-week bounds, limit=1.",
    },
    {
        "id": "Q13_combined_filters",
        "prompt": "SBI auctions in Chennai under 30 lakhs starting next week.",
        "expect_nonempty": False,
        "prefer_tool": "search_auctions",
        "note": "Combined structured filters; may legitimately return zero.",
    },
    {
        "id": "Q14_same_calendar_day",
        "prompt": "For auction 738522, which other auctions are happening on the same calendar day?",
        "expect_nonempty": False,
        "prefer_tool": None,
        "note": "Tests date(dt) calendar equality.",
    },
    {
        "id": "Q15_earliest_latest",
        "prompt": "What's the earliest and latest auction date in the dataset?",
        "expect_nonempty": True,
        "prefer_tool": None,
        "note": "Tests min/max + (post-fix) deadline_desc.",
    },
]


def _row_count(result: Any) -> int:
    """Best-effort row count across the various tool result shapes."""
    if result is None:
        return 0
    if isinstance(result, dict):
        if "results" in result and isinstance(result["results"], list):
            return len(result["results"])
        if "total_count" in result and isinstance(result["total_count"], int):
            return result["total_count"]
        if "rows" in result and isinstance(result["rows"], list):
            return len(result["rows"])
        return 1
    if isinstance(result, list):
        return len(result)
    return 1


_REFUSAL_PATTERNS = (
    "i cannot",
    "i am sorry",
    "i'm sorry",
    "i am unable",
    "i'm unable",
    "i encountered an error",
    "the available tools do not",
    "the tools do not",
    "do not have the functionality",
    "would you like that",
    "would you like me to",
    "can you provide",
    "please provide",
)


def _looks_like_refusal(answer: str) -> bool:
    """Heuristic: agent declined to answer or punted back to the user."""
    a = answer.lower()
    return any(p in a for p in _REFUSAL_PATTERNS)


def _classify(question: dict[str, Any], answer: str, artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a verdict dict for one question."""
    tools = [a.get("tool") for a in artifacts if a.get("tool")]
    primary_tool = tools[0] if tools else None
    max_rows = max((_row_count(a.get("result")) for a in artifacts), default=0)
    answer_text = (answer or "").strip()

    if not answer_text:
        verdict = "FAIL"
        why = "agent returned no answer"
    elif not tools and _looks_like_refusal(answer_text):
        verdict = "FAIL"
        why = "agent refused / punted without calling any tool"
    elif not tools and question["expect_nonempty"]:
        verdict = "FAIL"
        why = "no tool called for a question that needs live data"
    elif question["expect_nonempty"] and max_rows == 0 and tools:
        verdict = "FAIL"
        why = f"tool {primary_tool} returned 0 rows but data should exist"
    elif (
        question["prefer_tool"]
        and primary_tool
        and primary_tool != question["prefer_tool"]
        and "run_cypher" in tools
    ):
        verdict = "PARTIAL"
        why = f"used run_cypher; {question['prefer_tool']} would use the index"
    else:
        verdict = "PASS"
        why = f"answered via {primary_tool or 'no tool'}"

    return {
        "id": question["id"],
        "prompt": question["prompt"],
        "verdict": verdict,
        "why": why,
        "primary_tool": primary_tool,
        "all_tools": tools,
        "max_rows": max_rows,
        "answer_excerpt": answer_text[:400],
    }


def run_eval(base_url: str, tag: str, timeout: float, sleep_between: float) -> None:
    results: list[dict[str, Any]] = []
    print(f"\n=== Temporal-query eval (tag={tag}) ===")
    print(f"target: {base_url}/chat")
    print(f"questions: {len(QUESTIONS)}")
    print()

    with httpx.Client(timeout=timeout) as client:
        for q in QUESTIONS:
            t0 = time.time()
            try:
                resp = client.post(
                    f"{base_url}/chat",
                    json={"message": q["prompt"], "message_history": None, "mode": None},
                )
                resp.raise_for_status()
                payload = resp.json()
            except httpx.HTTPStatusError as exc:
                results.append({
                    "id": q["id"],
                    "prompt": q["prompt"],
                    "verdict": "ERROR",
                    "why": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
                    "primary_tool": None,
                    "all_tools": [],
                    "max_rows": 0,
                    "answer_excerpt": "",
                })
                print(f"[{q['id']}] ERROR  HTTP {exc.response.status_code}")
                continue
            except Exception as exc:
                results.append({
                    "id": q["id"],
                    "prompt": q["prompt"],
                    "verdict": "ERROR",
                    "why": f"{type(exc).__name__}: {exc}",
                    "primary_tool": None,
                    "all_tools": [],
                    "max_rows": 0,
                    "answer_excerpt": "",
                })
                print(f"[{q['id']}] ERROR  {type(exc).__name__}")
                continue

            verdict = _classify(q, payload.get("answer", ""), payload.get("artifacts", []))
            verdict["latency_s"] = round(time.time() - t0, 2)
            results.append(verdict)

            tag_str = {"PASS": "PASS ", "PARTIAL": "PART.", "FAIL": "FAIL ", "ERROR": "ERR  "}[verdict["verdict"]]
            print(f"[{q['id']}] {tag_str}  tool={verdict['primary_tool'] or '-':<22}  rows={verdict['max_rows']:<4}  ({verdict['latency_s']}s)")
            print(f"       why: {verdict['why']}")
            if sleep_between > 0:
                time.sleep(sleep_between)

    counts = {"PASS": 0, "PARTIAL": 0, "FAIL": 0, "ERROR": 0}
    for r in results:
        counts[r["verdict"]] += 1
    print()
    print(f"=== Summary (tag={tag}) ===")
    print(f"  PASS    : {counts['PASS']:>2} / {len(QUESTIONS)}")
    print(f"  PARTIAL : {counts['PARTIAL']:>2} / {len(QUESTIONS)}")
    print(f"  FAIL    : {counts['FAIL']:>2} / {len(QUESTIONS)}")
    print(f"  ERROR   : {counts['ERROR']:>2} / {len(QUESTIONS)}")

    out_path = Path(__file__).parent / f"_eval_temporal_{tag}.json"
    out_path.write_text(json.dumps({
        "tag": tag,
        "base_url": base_url,
        "summary": counts,
        "results": results,
    }, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://localhost:8000",
                   help="Where the FastAPI server is running (default: http://localhost:8000)")
    p.add_argument("--tag", required=True, help="Tag for the output file (e.g. baseline, post-fix)")
    p.add_argument("--timeout", type=float, default=120.0,
                   help="Per-request timeout in seconds (the agent can be slow)")
    p.add_argument("--sleep", type=float, default=1.0,
                   help="Sleep between questions to give the agent breathing room")
    args = p.parse_args()
    try:
        run_eval(args.base_url, args.tag, args.timeout, args.sleep)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
