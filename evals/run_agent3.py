"""
evals/run_agent3.py
-------------------
Run the agent3 tool catalogue against the live graph.

    python -m evals.run_agent3                     # everything
    python -m evals.run_agent3 --suite scope_honesty
    python -m evals.run_agent3 --case cap_sqft_filter -v
    python -m evals.run_agent3 --json runs/agent3.json

Needs NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD. Behind an HTTP-only egress
proxy (no Bolt) set NEO4J_HTTP_API=1.

No model is involved — this scores the tools, not an agent. A failure is a
data or tool bug. Exit code is non-zero when any suite misses its gate, so it
can sit in CI once the graph is reachable from there.

Every case's result is additionally run through `INVARIANTS`, so a case can
pass its own check and still fail the run for emitting a scope violation. That
is deliberate: the scope rule has to hold on results nobody wrote a case for.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from evals.agent3_cases import ALL_CASES, GATES, INVARIANTS, Case

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _call(case: Case) -> dict:
    # Imported inside the function so `--help` and the unit tests do not need
    # a Neo4j driver on the path.
    from api.agent3.benchmark_price import benchmark_price
    from api.agent3.find_by_identifier import find_by_identifier
    from api.agent3.find_properties import find_properties
    from api.agent3.get_property import get_property
    from api.agent3.reauction_history import reauction_history
    from api.agent3.search_notices import search_notices

    tools = {"find_properties": find_properties, "get_property": get_property,
             "find_by_identifier": find_by_identifier,
             "search_notices": search_notices,
             "benchmark_price": benchmark_price,
             "reauction_history": reauction_history}
    fn = tools.get(case.tool)
    if fn is None:
        raise KeyError(f"case {case.id} names an unknown tool {case.tool!r}")
    return fn(**case.args)


def _fixture_missing(case: Case, result: dict) -> bool:
    """True when the case's fixture listing is no longer in the graph.

    A fixture that has been re-scraped away is a data change, not a
    regression, and failing the run for it would train everyone to ignore
    red. Skipping keeps the signal honest.
    """
    if not case.fixture:
        return False
    if case.tool == "get_property":
        return case.fixture in (result.get("not_found") or [])
    return False


def run_case(case: Case) -> dict:
    started = time.perf_counter()
    try:
        result = _call(case)
    except Exception as exc:  # noqa: BLE001 - a crash is a result worth reporting
        return {"id": case.id, "suite": case.suite, "status": "ERROR",
                "problems": [f"{type(exc).__name__}: {exc}"],
                "seconds": round(time.perf_counter() - started, 2)}

    elapsed = round(time.perf_counter() - started, 2)
    if _fixture_missing(case, result):
        return {"id": case.id, "suite": case.suite, "status": "SKIP",
                "problems": [f"fixture {case.fixture} is no longer in the graph"],
                "seconds": elapsed}

    problems = list(case.check(result))
    invariant_problems: list[str] = []
    for inv in INVARIANTS:
        invariant_problems.extend(inv(result))
    problems.extend(f"[invariant] {p}" for p in invariant_problems)

    return {"id": case.id, "suite": case.suite,
            "status": "PASS" if not problems else "FAIL",
            "problems": problems, "seconds": elapsed,
            "question": case.question,
            "total_count": result.get("total_count"),
            "result": result}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--suite", help="run one suite only")
    ap.add_argument("--case", help="run one case by id")
    ap.add_argument("--tag", help="run cases carrying this tag")
    ap.add_argument("--json", dest="json_path", help="write the full report here")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print each case's result payload")
    args = ap.parse_args(argv)

    cases = ALL_CASES
    if args.suite:
        cases = [c for c in cases if c.suite == args.suite]
    if args.case:
        cases = [c for c in cases if c.id == args.case]
    if args.tag:
        cases = [c for c in cases if args.tag in c.tags]
    if not cases:
        print("no cases matched", file=sys.stderr)
        return 2

    results = []
    for case in cases:
        r = run_case(case)
        results.append(r)
        mark = {"PASS": "ok  ", "FAIL": "FAIL", "ERROR": "ERR ", "SKIP": "skip"}[r["status"]]
        count = r.get("total_count")
        suffix = f"  ({count} matches)" if count is not None else ""
        print(f"{mark} {r['suite']:<14} {r['id']:<42} {r['seconds']:>5.2f}s{suffix}")
        for p in r["problems"]:
            print(f"       - {p}")
        if args.verbose and r.get("result") is not None:
            print(json.dumps(r["result"], indent=2, default=str)[:4000])

    print()
    failed_gates = []
    for suite in sorted({r["suite"] for r in results}):
        rows = [r for r in results if r["suite"] == suite]
        scored = [r for r in rows if r["status"] != "SKIP"]
        passed = sum(1 for r in scored if r["status"] == "PASS")
        skipped = len(rows) - len(scored)
        rate = passed / len(scored) if scored else 1.0
        gate = GATES.get(suite, 0.0)
        ok = rate >= gate
        if not ok:
            failed_gates.append(suite)
        print(f"{suite:<16} {passed}/{len(scored)} "
              f"({rate:.0%}, gate {gate:.0%}) {'PASS' if ok else 'BELOW GATE'}"
              f"{f' · {skipped} skipped' if skipped else ''}")

    total_seconds = sum(r["seconds"] for r in results)
    print(f"\n{len(results)} cases · {total_seconds:.1f}s total")

    if args.json_path:
        out = Path(args.json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(
            {"cases": results, "gates": GATES}, indent=2, default=str))
        print(f"report written to {out}")

    if failed_gates:
        print(f"\nBELOW GATE: {', '.join(failed_gates)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
