"""
evals/run_loop_ab.py
--------------------
Score the **tiered loop** (`api/chat/v2`) and the **deep loop**
(`api/chat/deep`) on the same catalogues, in one run, and print them side by
side.

Why this exists: the original spike
(`experiments/deepagent-chat/README.md`) compared the two shapes on a
**4-question smoke set** and a vanilla harness with slim instructions. That
was enough to justify building the tiered loop and it is not enough to keep
choosing it. This runs both loops through the production tool surface, the
production policy, and the full 68-case golden catalogue plus the 9 scripted
conversations — so the comparison is between two loops rather than between a
spike and a product.

Two gates, the same ones the production eval uses:

* **trajectory** — a passing answer routes through one of the case's
  `acceptable_tools`;
* **refusal** — a refusal case declines gracefully (a `_DECLINE_MARKERS`
  phrase) instead of fabricating.

Plus, for conversations, the assertion the single-turn catalogue is
structurally blind to: **reference resolution** — a follow-up referring to
what an earlier answer NAMED must not come back asking the user to restate
it (`Turn.forbid_answer_markers`).

Reported honestly rather than reduced to one number: per-loop pass counts,
median and p90 wall clock, model calls, and input tokens — including the
cached share, because the tiered loop's cost case rests on a warm prefix and
the deep loop's rests on the provider caching a growing transcript. A loop
that wins on accuracy and loses 5x on cost is a decision, not a verdict.

    # needs OPENROUTER_CHAT_API_KEY (or OPENROUTER_API_KEY) + NEO4J_*
    python -m evals.run_loop_ab                     # everything
    python -m evals.run_loop_ab --suite golden      # 68 single-turn cases
    python -m evals.run_loop_ab --suite convo       # 9 conversations
    python -m evals.run_loop_ab --loop deep --limit 5
    python -m evals.run_loop_ab --json out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any

_REPO = str(Path(__file__).resolve().parents[1])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from evals.cases import GOLDEN, _DECLINE_MARKERS  # noqa: E402
from evals.conversations import GOLDEN_CONVERSATIONS  # noqa: E402

LOOPS = ("tiered", "deep")


def _turn_ceiling() -> float:
    from api.chat.deep.loop import TURN_TIMEOUT_S

    return TURN_TIMEOUT_S


_TURN_CEILING = _turn_ceiling()


# ── running one turn on either loop ─────────────────────────────────────────

async def _run_tiered(question: str, state: dict[str, Any]) -> dict[str, Any]:
    """One turn through the tiered loop, threading its scope object."""
    from api.chat.v2.executor import TurnBudget
    from api.chat.v2.loop import run_turn

    result = await run_turn(
        question,
        scope=state.get("filters") or {},
        last_ids=state.get("last_ids") or [],
        last_total_count=state.get("last_total_count"),
        last_question=state.get("last_question") or "",
        last_entities=state.get("last_entities") or {},
        budget=TurnBudget(),
    )
    # The client echoes this back; the runner stands in for the client.
    state.update(
        filters=result.filters,
        last_ids=result.last_ids,
        last_total_count=result.last_total_count,
        last_question=result.last_question,
        last_entities=result.last_entities,
    )
    return _harvest(result)


async def _run_deep(question: str, state: dict[str, Any]) -> dict[str, Any]:
    """One turn through the deep loop, threading only a thread_id.

    The whole state this loop needs across turns is that string — the
    transcript itself lives in the checkpointer.
    """
    from api.chat.deep.checkpointer import Neo4jSaver
    from api.chat.deep.loop import run_turn

    thread_id = state.setdefault("thread_id", f"eval-{uuid.uuid4()}")
    result = await run_turn(
        question, thread_id=thread_id, checkpointer=Neo4jSaver(),
    )
    return _harvest(result)


async def _forget_deep(state: dict[str, Any]) -> None:
    """Drop a scenario's thread so runs don't accumulate in the graph."""
    thread_id = state.get("thread_id")
    if not thread_id:
        return
    from api.chat.deep.checkpointer import Neo4jSaver

    try:
        await Neo4jSaver().adelete_thread(thread_id)
    except Exception as exc:  # noqa: BLE001 - cleanup must never fail a run
        print(f"  ! could not clean thread {thread_id}: {exc}")


def _harvest(result) -> dict[str, Any]:
    return {
        "answer": result.answer or "",
        "tools": [c.tool for c in result.executed],
        "tool_args": [c.args for c in result.executed],
        "seconds": result.seconds,
        "model_calls": result.model_calls,
        "input_tokens": result.input_tokens,
        "cached_tokens": result.cached_tokens,
        "output_tokens": result.output_tokens,
        "gate_ok": bool(getattr(result, "gate", None) is None
                        or result.gate.ok),
    }


_RUNNERS = {"tiered": _run_tiered, "deep": _run_deep}


# ── scoring ─────────────────────────────────────────────────────────────────

def _gave_up(answer: str) -> str | None:
    """Did the loop itself fail to produce an answer? Returns why, or None.

    This check has to come FIRST in both scorers, and the smoke run is why.
    A deep-loop turn hit the 120 s ceiling and returned its timeout text — but
    `search_auctions` had already run, so a trajectory-only score called it a
    **pass**. The A/B would have reported a loop that failed to answer as
    passing, which is worse than not running the A/B at all.

    The sentinels are imported, never copied: if a loop reworks its failure
    text, this goes stale silently and the false pass comes back.
    """
    from api.chat.deep.loop import _FAILED, _NO_ANSWER, _TIMED_OUT
    from api.chat.v2.loop import _CANT_ANSWER, _CANT_PLAN

    return {
        _TIMED_OUT: f"gave up: turn exceeded the {int(_TURN_CEILING)}s ceiling",
        _FAILED: "gave up: the graph raised",
        _NO_ANSWER: "gave up: tools ran but no answer was written",
        _CANT_PLAN: "gave up: could not plan the lookup",
        _CANT_ANSWER: "gave up: could not write the results up",
    }.get((answer or "").strip())


def _score_golden(case, turn: dict[str, Any]) -> tuple[str, str]:
    """Returns (verdict, note), same shape as `_score_turn`."""
    gave_up = _gave_up(turn["answer"])
    if gave_up:
        return "FAIL", gave_up
    answer = (turn["answer"] or "").lower()
    if case.expect_refusal:
        if any(m in answer for m in _DECLINE_MARKERS):
            return "pass", ""
        return "FAIL", "refusal case answered without a decline marker"
    used = set(turn["tools"])
    if used & set(case.acceptable_tools):
        return "pass", ""
    if not used and answer:
        # Answered without tools. Informational, not a trajectory pass — the
        # spike reported these separately rather than burying them either way.
        return "direct", ""
    return "FAIL", (f"expected one of {case.acceptable_tools}, "
                    f"called {sorted(used) or 'nothing'}")


def _score_turn(turn_spec, turn: dict[str, Any]) -> tuple[str, str]:
    """Returns (verdict, note). Conversation turns carry more assertions."""
    gave_up = _gave_up(turn["answer"])
    if gave_up:
        return "FAIL", gave_up
    answer = (turn["answer"] or "").lower()
    for marker in turn_spec.forbid_answer_markers:
        if marker in answer:
            return "FAIL", f"bounced the reference back: {marker!r}"
    if turn_spec.expected_tools:
        if not set(turn["tools"]) & set(turn_spec.expected_tools):
            return "FAIL", (f"expected one of {turn_spec.expected_tools}, "
                            f"called {turn['tools'] or 'nothing'}")
    return "pass", ""


# ── suites ──────────────────────────────────────────────────────────────────

async def _run_golden(loop: str, limit: int | None) -> list[dict[str, Any]]:
    cases = GOLDEN[:limit] if limit else GOLDEN
    rows = []
    for idx, case in enumerate(cases, 1):
        state: dict[str, Any] = {}
        started = time.perf_counter()
        try:
            turn = await _RUNNERS[loop](case.question, state)
            verdict, note = _score_golden(case, turn)
        except Exception as exc:  # noqa: BLE001
            turn = {"answer": "", "tools": [], "seconds": 0, "model_calls": 0,
                    "input_tokens": 0, "cached_tokens": 0, "output_tokens": 0,
                    "gate_ok": True}
            verdict, note = "ERROR", str(exc)[:200]
        if loop == "deep":
            await _forget_deep(state)
        rows.append({"suite": "golden", "loop": loop, "id": case.intent,
                     "question": case.question, "verdict": verdict,
                     "note": note, **turn,
                     "wall": round(time.perf_counter() - started, 1)})
        print(f"  [{loop}] {idx}/{len(cases)} {verdict:6} "
              f"{turn['seconds']:5.1f}s  {case.question[:58]}")
    return rows


async def _run_convos(loop: str, limit: int | None) -> list[dict[str, Any]]:
    convos = GOLDEN_CONVERSATIONS[:limit] if limit else GOLDEN_CONVERSATIONS
    rows = []
    for conv in convos:
        state: dict[str, Any] = {}
        print(f"  [{loop}] {conv.conv_id}")
        for turn_no, turn_spec in enumerate(conv.turns, 1):
            try:
                turn = await _RUNNERS[loop](turn_spec.message, state)
                verdict, note = _score_turn(turn_spec, turn)
            except Exception as exc:  # noqa: BLE001
                turn = {"answer": "", "tools": [], "seconds": 0,
                        "model_calls": 0, "input_tokens": 0,
                        "cached_tokens": 0, "output_tokens": 0,
                        "gate_ok": True}
                verdict, note = "ERROR", str(exc)[:200]
            rows.append({"suite": "convo", "loop": loop,
                         "id": f"{conv.conv_id}#{turn_no}",
                         "question": turn_spec.message, "verdict": verdict,
                         "note": note, **turn})
            flag = f"  <- {note}" if note else ""
            print(f"      t{turn_no} {verdict:6} {turn['seconds']:5.1f}s  "
                  f"{turn_spec.message[:48]}{flag}")
        if loop == "deep":
            await _forget_deep(state)
    return rows


# ── reporting ───────────────────────────────────────────────────────────────

def _summarise(rows: list[dict[str, Any]]) -> None:
    print("\n" + "=" * 78)
    for suite in ("golden", "convo"):
        subset = [r for r in rows if r["suite"] == suite]
        if not subset:
            continue
        print(f"\n{suite.upper()}")
        print(f"{'loop':8} {'pass':>5} {'direct':>7} {'FAIL':>5} {'ERR':>4} "
              f"{'med s':>7} {'p90 s':>7} {'calls':>6} {'in tok':>8} "
              f"{'cached':>7}")
        for loop in LOOPS:
            got = [r for r in subset if r["loop"] == loop]
            if not got:
                continue
            secs = sorted(r["seconds"] for r in got) or [0]
            print(
                f"{loop:8} "
                f"{sum(1 for r in got if r['verdict'] == 'pass'):>5} "
                f"{sum(1 for r in got if r['verdict'] == 'direct'):>7} "
                f"{sum(1 for r in got if r['verdict'] == 'FAIL'):>5} "
                f"{sum(1 for r in got if r['verdict'] == 'ERROR'):>4} "
                f"{statistics.median(secs):>7.1f} "
                f"{secs[min(len(secs) - 1, int(len(secs) * 0.9))]:>7.1f} "
                f"{statistics.mean([r['model_calls'] for r in got]):>6.2f} "
                f"{statistics.mean([r['input_tokens'] for r in got]):>8.0f} "
                f"{statistics.mean([r['cached_tokens'] for r in got]):>7.0f}"
            )
    failures = [r for r in rows if r["verdict"] in ("FAIL", "ERROR")]
    if failures:
        print(f"\n{len(failures)} failure(s):")
        for r in failures:
            print(f"  [{r['loop']}] {r['suite']}/{r['id']}: {r['question'][:56]}")
            if r["note"]:
                print(f"      {r['note']}")
    print("\n" + "=" * 78)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=("golden", "convo", "all"), default="all")
    ap.add_argument("--loop", choices=(*LOOPS, "both"), default="both")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--json", type=str, default=None)
    args = ap.parse_args()

    if not (os.getenv("OPENROUTER_CHAT_API_KEY") or os.getenv("OPENROUTER_API_KEY")):
        sys.exit("set OPENROUTER_CHAT_API_KEY (or OPENROUTER_API_KEY) first")

    loops = LOOPS if args.loop == "both" else (args.loop,)
    suites = ("golden", "convo") if args.suite == "all" else (args.suite,)

    async def _go() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for suite in suites:
            for loop in loops:
                print(f"\n--- {suite} / {loop} ---")
                runner = _run_golden if suite == "golden" else _run_convos
                rows.extend(await runner(loop, args.limit))
        return rows

    rows = asyncio.run(_go())
    _summarise(rows)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2, default=str))
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
