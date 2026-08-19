"""Run the golden-question catalogue (evals/cases.py) through variant B and
score it on the same two gates the production eval uses:

- **trajectory** — a passing answer must route through one of the case's
  `acceptable_tools` (production name `get_auction_detail` aliased to the
  spike's `get_auction_details`);
- **refusal** — refusal cases must contain a decline marker and no
  fabricated data tool call is required.

Two honest scope gaps, reported rather than hidden:
- The spike has no `describe_schema`/`run_cypher` (tier 3 isn't built), so
  `schema`-intent cases can only pass if another acceptable tool suffices.
- `internet_search` is registered only when TAVILY_API_KEY is set; without
  it, off-graph questions answer via the planner's direct_answer and are
  scored as `direct` (informational, not a trajectory pass).

    python experiments/deepagent-chat/run_golden_b.py            # all 68
    python experiments/deepagent-chat/run_golden_b.py --limit 6  # first N
    python experiments/deepagent-chat/run_golden_b.py --intent multi_hop
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
_REPO = str(Path(__file__).resolve().parents[2])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from evals.cases import GOLDEN, _DECLINE_MARKERS  # noqa: E402
from variant_b import run as run_b  # noqa: E402

_ALIAS = {"get_auction_detail": "get_auction_details"}


def _score(case, result: dict) -> str:
    used = set(result.get("tools", []))
    answer = (result.get("answer") or "").lower()
    if case.expect_refusal:
        return "pass" if any(m in answer for m in _DECLINE_MARKERS) else "FAIL"
    acceptable = {_ALIAS.get(t, t) for t in case.acceptable_tools}
    if used & acceptable:
        return "pass"
    if not used and answer:
        return "direct"  # answered without tools — judge by hand
    return "FAIL"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--intent", type=str, default=None)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    cases = [c for c in GOLDEN if not args.intent or c.intent == args.intent]
    if args.limit:
        cases = cases[: args.limit]

    def one(idx_case):
        i, case = idx_case
        t0 = time.perf_counter()
        try:
            r = run_b(case.question)
        except Exception as e:  # keep the sweep alive
            r = {"tools": [], "answer": f"ERROR: {e}", "llm_calls": -1,
                 "seconds": round(time.perf_counter() - t0, 1),
                 "input_tokens": -1, "output_tokens": -1}
        verdict = _score(case, r)
        print(f"[{i+1:>2}/{len(cases)}] {verdict:<6} {case.intent:<16} "
              f"{r['seconds']:>5}s {r['llm_calls']}c  {case.question[:60]}",
              flush=True)
        return {"intent": case.intent, "question": case.question,
                "verdict": verdict, "expected": case.acceptable_tools,
                **{k: r[k] for k in ("seconds", "llm_calls", "input_tokens",
                                     "output_tokens", "tools", "answer")}}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(one, enumerate(cases)))

    n = len(rows)
    passes = sum(1 for r in rows if r["verdict"] == "pass")
    directs = sum(1 for r in rows if r["verdict"] == "direct")
    fails = [r for r in rows if r["verdict"] == "FAIL"]
    ok_rows = [r for r in rows if r["llm_calls"] > 0]
    avg_s = sum(r["seconds"] for r in ok_rows) / max(1, len(ok_rows))
    avg_c = sum(r["llm_calls"] for r in ok_rows) / max(1, len(ok_rows))
    avg_in = sum(r["input_tokens"] for r in ok_rows) / max(1, len(ok_rows))

    print(f"\n== variant B on golden catalogue ==")
    print(f"cases {n} | pass {passes} | direct {directs} | FAIL {len(fails)}")
    print(f"avg {avg_s:.1f}s | {avg_c:.2f} model calls | {avg_in:.0f} in tokens")

    by_intent: dict[str, list] = {}
    for r in rows:
        by_intent.setdefault(r["intent"], []).append(r)
    print(f"\n{'intent':<17}{'n':>3}{'pass':>6}{'direct':>7}{'fail':>6}{'avg s':>7}{'calls':>7}")
    for intent, rs in sorted(by_intent.items()):
        p = sum(1 for r in rs if r["verdict"] == "pass")
        d = sum(1 for r in rs if r["verdict"] == "direct")
        f = sum(1 for r in rs if r["verdict"] == "FAIL")
        s = sum(r["seconds"] for r in rs) / len(rs)
        c = sum(max(0, r["llm_calls"]) for r in rs) / len(rs)
        print(f"{intent:<17}{len(rs):>3}{p:>6}{d:>7}{f:>6}{s:>7.1f}{c:>7.2f}")

    if fails:
        print("\n-- failures --")
        for r in fails:
            print(f"[{r['intent']}] {r['question'][:70]}")
            print(f"   expected {r['expected']} got {r['tools']}")
            print(f"   answer: {str(r['answer'])[:160]}")

    out = Path(__file__).parent / "golden_b_results.json"
    out.write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
