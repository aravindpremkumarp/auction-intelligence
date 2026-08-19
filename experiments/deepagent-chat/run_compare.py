"""Run the question set through both variants and print a comparison table.

    python experiments/deepagent-chat/run_compare.py            # smoke set
    python experiments/deepagent-chat/run_compare.py --all
    python experiments/deepagent-chat/run_compare.py --variant b --q 2
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

QUESTIONS = [
    # (id, tier-expectation, question)
    (1, "T1 filter",    "Show me residential auctions in Chennai under 30 lakhs"),
    (2, "T1 aggregate", "What's the price range and median reserve price in Karur?"),
    (3, "T1 semantic",  "Any properties near a school or college with road frontage?"),
    (4, "T2 multi-hop", "Find flats under 25 lakhs in Chennai that failed to sell "
                        "at an earlier auction, and compare the two cheapest."),
    (5, "off-graph",    "What does symbolic possession under SARFAESI mean?"),
    (6, "T1 detail",    "Which bank has the most live auctions, and what share are re-auctions?"),
]
SMOKE_IDS = {1, 2, 4, 5}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--variant", choices=["a", "b", "both"], default="both")
    ap.add_argument("--q", type=int, default=None, help="run a single question id")
    ap.add_argument("--json", action="store_true", help="dump raw results as JSON")
    args = ap.parse_args()

    qs = [q for q in QUESTIONS
          if (args.q and q[0] == args.q)
          or (not args.q and (args.all or q[0] in SMOKE_IDS))]

    runners = {}
    if args.variant in ("a", "both"):
        from variant_a import run as run_a
        runners["A"] = run_a
    if args.variant in ("b", "both"):
        from variant_b import run as run_b
        runners["B"] = run_b

    rows = []
    for qid, tier, question in qs:
        for name, runner in runners.items():
            print(f"\n=== Q{qid} [{tier}] variant {name} ===", flush=True)
            try:
                r = runner(question)
            except Exception:
                traceback.print_exc()
                r = {"variant": name, "seconds": -1, "llm_calls": -1,
                     "input_tokens": -1, "output_tokens": -1, "tools": [],
                     "answer": "ERROR (see traceback)"}
            r.update({"q": qid, "tier": tier, "question": question})
            rows.append(r)
            print(f"    {r['seconds']}s | {r['llm_calls']} model calls | "
                  f"in {r['input_tokens']} out {r['output_tokens']} | "
                  f"tools {r['tools']}")
            print(f"    answer: {str(r['answer'])[:220]}")

    print("\n\n| Q | tier | variant | s | model calls | in tok | out tok | tools |")
    print("|---|------|---------|---|-------------|--------|---------|-------|")
    for r in rows:
        print(f"| {r['q']} | {r['tier']} | {r['variant']} | {r['seconds']} "
              f"| {r['llm_calls']} | {r['input_tokens']} | {r['output_tokens']} "
              f"| {', '.join(r['tools']) or '—'} |")

    if args.json:
        out = Path(__file__).parent / "results.json"
        out.write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
        print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
