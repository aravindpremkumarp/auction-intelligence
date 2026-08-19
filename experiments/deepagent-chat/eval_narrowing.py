"""Narrowing-conversation eval for the tiered variant.

Tests the scope-object design: a buyer starts broad and narrows through
continued questioning. Scoring is fully programmatic — no LLM judge:

- **carry**: each turn's executed search_auctions args must contain every
  filter the conversation has accumulated so far (the deterministic merge
  is the feature under test);
- **shrink**: total_count must be non-increasing across narrowing turns;
- **anchor**: a final "the cheapest one / details" turn must call
  get_auction_details with an id from the previous turn's results.

    python experiments/deepagent-chat/eval_narrowing.py
    python experiments/deepagent-chat/eval_narrowing.py --scenario 1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from variant_b import run as run_b  # noqa: E402


def _check_carry(executed: list[dict], expect: dict) -> tuple[bool, str]:
    """Every expected filter key must appear (with a matching-ish value) in
    at least one executed search_auctions call this turn."""
    searches = [e for e in executed if e.get("tool") == "search_auctions"]
    if not searches:
        return False, "no search_auctions executed"
    for k, want in expect.items():
        ok = False
        for e in searches:
            got = (e.get("args") or {}).get(k)
            if got is None:
                continue
            if isinstance(want, str):
                g = json.dumps(got).lower()
                if want.lower() in g:
                    ok = True
            elif isinstance(want, bool):
                ok = ok or got is want
            else:  # numeric: allow the model reasonable leeway (lakh vs raw)
                try:
                    ok = ok or abs(float(got) - float(want)) / max(float(want), 1) < 0.01
                except (TypeError, ValueError):
                    pass
            if ok:
                break
        if not ok:
            return False, f"filter {k}={want!r} not carried (searches: {[e.get('args') for e in searches]})"
    return True, ""


# Each turn: question, the filters that must be live on the executed search
# AFTER this turn (accumulation is the point), and flags for the two other
# checks. `detail_from_prior` marks the anchor turn.
SCENARIOS = [
    {
        "name": "price-and-place narrowing (Chennai)",
        "turns": [
            {"q": "Show me residential properties for auction",
             "carry": {}},
            {"q": "Only in Chennai please",
             "carry": {"city": "chennai"}, "shrink": True},
            {"q": "Under 40 lakhs",
             "carry": {"city": "chennai", "max_price": 4000000}, "shrink": True},
            {"q": "Of these, only ones that failed to sell at an earlier auction",
             "carry": {"city": "chennai", "max_price": 4000000, "is_reauction": True},
             "shrink": True},
            {"q": "Give me the full details of the cheapest one",
             "detail_from_prior": True},
        ],
    },
    {
        "name": "bank narrowing (drop a filter mid-way)",
        "turns": [
            {"q": "What live auctions does Indian Bank have?",
             "carry": {"bank": "indian bank"}},
            {"q": "Only flats",
             "carry": {"bank": "indian bank"}, "shrink": True},
            {"q": "Actually any property type is fine, but only in Coimbatore",
             "carry": {"bank": "indian bank", "city": "coimbatore"}},
        ],
    },
]


def run_scenario(sc: dict) -> dict:
    state: dict = {}
    rows = []
    prev_count: int | None = None
    prior_ids: list[str] = []
    for i, turn in enumerate(sc["turns"], 1):
        r = run_b(turn["q"], state=state)
        state = r["state"]
        checks: list[str] = []
        ok = True

        if turn.get("carry"):
            c_ok, why = _check_carry(r["executed"], turn["carry"])
            checks.append("carry:" + ("PASS" if c_ok else f"FAIL({why})"))
            ok &= c_ok

        count = state.get("last_total_count")
        if turn.get("shrink"):
            s_ok = (prev_count is None or count is None or count <= prev_count)
            checks.append(f"shrink:{'PASS' if s_ok else 'FAIL'}({prev_count}->{count})")
            ok &= s_ok

        if turn.get("detail_from_prior"):
            detail_calls = [e for e in r["executed"]
                            if e.get("tool") == "get_auction_details"]
            used = [i_ for e in detail_calls
                    for i_ in (e.get("args") or {}).get("auction_ids", [])]
            d_ok = bool(used) and all(u in prior_ids for u in used)
            checks.append("anchor:" + ("PASS" if d_ok else
                          f"FAIL(used={used[:3]}, prior={len(prior_ids)} ids)"))
            ok &= d_ok

        rows.append({
            "turn": i, "q": turn["q"], "ok": ok, "checks": checks,
            "seconds": r["seconds"], "llm_calls": r["llm_calls"],
            "input_tokens": r["input_tokens"], "tools": r["tools"],
            "count": count,
            "answer": str(r["answer"])[:160],
        })
        prev_count = count if count is not None else prev_count
        prior_ids = list(state.get("last_ids") or [])
        print(f"  T{i} {'PASS' if ok else 'FAIL'} "
              f"[{' '.join(checks) or 'no-checks'}] {r['seconds']}s "
              f"{r['llm_calls']} calls in={r['input_tokens']} "
              f"count={count} tools={r['tools']}", flush=True)
    return {"name": sc["name"], "turns": rows,
            "passed": all(t["ok"] for t in rows)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", type=int, default=None)
    args = ap.parse_args()
    out = []
    for n, sc in enumerate(SCENARIOS, 1):
        if args.scenario and n != args.scenario:
            continue
        print(f"\n=== Scenario {n}: {sc['name']} ===", flush=True)
        out.append(run_scenario(sc))
    passed = sum(1 for s in out if s["passed"])
    print(f"\nScenarios passed: {passed}/{len(out)}")
    path = Path(__file__).parent / "narrowing_results.json"
    path.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
