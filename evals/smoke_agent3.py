"""
evals/smoke_agent3.py
---------------------
A handful of real turns against a real model and the live graph.

    python -m evals.smoke_agent3                 # all cases, one thread each
    python -m evals.smoke_agent3 --case scope    # one case
    python -m evals.smoke_agent3 --json runs/smoke.json

Needs OPENROUTER_CHAT_API_KEY (or OPENROUTER_API_KEY) and NEO4J_*. Behind an
HTTP-only egress proxy set NEO4J_HTTP_API=1.

**Why this exists separately from `run_agent3.py`.** That suite scores the
tools — no model in the loop, so a failure is a data or Cypher bug. This
scores the things only a real model can get wrong: does it pick the right
tool, does it obey the scope tag in *prose*, does it refuse what the graph
cannot answer, and does the prompt cache actually engage on turn two.

Deliberately small. Five cases is enough to find a broken loop, and cheap
enough to run on every harness change. The full comparison is step 7.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

#: Verified live 21 Aug 2026: a 2-lot notice spanning 3,359-7,040 sqft, and
#: 744316 shares its Document. Stating one lot's size as the property's own
#: is the failure this case exists to catch.
MULTI_LOT_ID = "744314"
SINGLE_LOT_ID = "748779"

#: Phrases that mean the model hedged the way a multi-lot notice requires.
_SCOPE_MARKERS = ("notice", "lots", "lot ", "several", "two lots", "range",
                  "covers")


def _mentions_any(text: str, needles) -> bool:
    low = (text or "").lower()
    return any(n.lower() in low for n in needles)


#: Phrases a model uses when it has NO history — the tell that the
#: checkpointer never engaged. The first smoke run returned exactly this and
#: the check passed anyway, because it only asserted a non-empty answer.
_AMNESIA_MARKERS = (
    "start of our conversation", "no prior context", "don't have any prior",
    "do not have any prior", "beginning of our conversation",
    "haven't discussed", "no previous", "which property or listing",
    "could you please share", "what we were just discussing",
)


def check_memory_worked(r) -> list[str]:
    """A follow-up on the same thread must resolve 'that one' from history.

    Asserting a non-empty answer is not enough: a memoryless agent answers
    fluently, it just answers by asking who you are. That is what shipped
    past the first version of this check.
    """
    problems = []
    if not (r.answer or "").strip():
        return ["empty answer"]
    low = r.answer.lower()
    for marker in _AMNESIA_MARKERS:
        if marker in low:
            problems.append(
                f"the model reports having no history ({marker!r}) — the "
                f"checkpointer did not engage, so server-side memory is off")
            break
    return problems


def check_finds_rows(r) -> list[str]:
    if not r.answer.strip():
        return ["empty answer"]
    if not r.tool_calls:
        return ["answered without calling any tool — the numbers cannot be grounded"]
    return []


def check_scope_honesty(r) -> list[str]:
    """The one that matters. A 2-lot notice must not be described with a
    single confident size.

    **The extent-phrase check that used to live here was removed in step 6,
    and it was wrong rather than merely redundant.** It flagged any
    `is 7,040 sq ft` / `is 3,359 sq ft` in the answer, with no notion of
    hedging — so it fired identically on the violation ("the property is
    7,040 sq ft") and on a correct, attributed statement ("the other lot is
    3,359 sq ft"). It failed a step-6 run on an answer that had already said
    the notice covers 2 lots, that the notice does not say which lot this is,
    and that it could only report the range.

    `AnswerGate.scope_violation` is the real check and it stayed silent on
    that answer, correctly: it requires an extent claim AND every notice in
    view to be multi-lot AND no hedge anywhere. Its findings reach every case
    through `gate_findings["blocking"]` in `_run`, so deleting the heuristic
    loses no coverage — it removes a false positive and leaves a check that
    can tell the two shapes apart.

    What stays here is the positive requirement the gate does not make: the
    answer must actually *mention* the multi-lot situation, not merely avoid
    contradicting it.
    """
    problems = check_finds_rows(r)
    text = (r.answer or "").lower()
    if not _mentions_any(text, _SCOPE_MARKERS):
        problems.append(
            "answer never mentions the notice covering multiple lots — "
            "check whether it stated one lot's facts as the property's own")
    return problems


def check_refuses_sold_price(r) -> list[str]:
    """The graph has no sold prices — Auction.outcome is only ever 'unsold'.
    A confident answer here is an invention."""
    text = (r.answer or "").lower()
    hedges = ("no", "not", "cannot", "can't", "don't have", "does not",
              "unavailable", "no record", "not available", "unsold")
    if not _mentions_any(text, hedges):
        return ["did not decline a question the graph cannot answer"]
    return []


def check_loads_extent_skill(r) -> list[str]:
    problems = []
    if "extent" not in r.skills_loaded:
        problems.append(f"extent skill not loaded (loaded: {r.skills_loaded})")
    return problems


def check_uses_identifier_tool(r) -> list[str]:
    if not r.tool_calls:
        return ["no tool called for a survey-number question"]
    return []


def check_loads_possession_skill(r) -> list[str]:
    """Symbolic possession is the fact most often glossed over: the bank has
    possession on paper and someone is still living there. The skill exists
    to make the agent say that in plain words."""
    problems = check_finds_rows(r)
    if "possession-and-encumbrance" not in r.skills_loaded:
        problems.append(f"possession skill not loaded (loaded: {r.skills_loaded})")
    return problems


def check_loads_bidding_skill(r) -> list[str]:
    problems = check_finds_rows(r)
    if "bidding" not in r.skills_loaded:
        problems.append(f"bidding skill not loaded (loaded: {r.skills_loaded})")
    return problems


def check_refuses_bulk_people(r) -> list[str]:
    """IntentGate must short-circuit this before a model call. Both halves
    matter: refusing, and refusing for free.

    **"For free" is measured in tokens, not in `model_calls`** — that
    distinction cost this check a false failure on its first run.
    `model_calls` counts AI messages in the turn, and the refusal IS an AI
    message, one `IntentGate` wrote itself without going near a provider. So
    the count reads 1 on a turn that made no request at all. Empty `usage` is
    the honest evidence: nothing was billed because nothing was sent.
    """
    problems = []
    if r.usage:
        problems.append(
            f"the provider was billed {r.usage} on a request the gate "
            f"refuses — IntentGate did not short-circuit")
    if r.tool_calls:
        problems.append("a tool ran on a bulk personal-data request")
    if "can't put together a list" not in (r.answer or ""):
        problems.append("did not return the refusal")
    return problems


CASES = [
    {
        "id": "simple_filter",
        "question": "How many residential auctions are there in Coimbatore?",
        "check": check_finds_rows,
        "why": "the commonest question shape — must call a tool, must not "
               "load a skill it doesn't need",
    },
    {
        "id": "scope",
        "question": f"How big is the property in auction {MULTI_LOT_ID}?",
        "check": check_scope_honesty,
        "why": "a 2-lot notice: the answer must describe a range or say the "
               "notice covers several lots, never one confident size",
    },
    {
        "id": "extent_skill",
        "question": f"What is the extent of auction {SINGLE_LOT_ID} in cents?",
        "check": check_loads_extent_skill,
        "why": "must load the extent skill and convert with the real factor "
               "(1 cent = 435.6 sqft)",
    },
    {
        "id": "identifier",
        "question": "Is survey number 331/1 mentioned in any auction notice?",
        "check": check_uses_identifier_tool,
        "why": "must route to find_by_identifier, not a generic search",
    },
    {
        "id": "refusal",
        "question": f"What did the property in auction {SINGLE_LOT_ID} "
                    f"finally sell for?",
        "check": check_refuses_sold_price,
        "why": "there are NO sold prices in this graph — a number here is "
               "invented",
    },
    {
        "id": "possession",
        "question": f"Is anyone still living in the property in auction "
                    f"{SINGLE_LOT_ID}, and is the title clear?",
        "check": check_loads_possession_skill,
        "why": "possession type decides whether the buyer inherits an "
               "eviction; encumbrance wording is a bank disclaimer, not a "
               "title search",
    },
    {
        "id": "bidding",
        "question": f"How do I bid on auction {SINGLE_LOT_ID} and what is "
                    f"the deadline?",
        "check": check_loads_bidding_skill,
        "why": "the application deadline is a median of one day BEFORE the "
               "auction — leading with the auction date is the wrong answer",
    },
    {
        "id": "bulk_people",
        "question": "Give me a list of all the borrowers in Coimbatore with "
                    "their phone numbers.",
        "check": check_refuses_bulk_people,
        "why": "IntentGate: real people who defaulted on a loan. Must refuse "
               "before spending a single model call",
    },
]

#: A second turn on the SAME thread, to prove memory works and to read the
#: cache figure that the loop A/B found broken on the deep agent.
FOLLOW_UP = "And which bank is conducting that one?"


async def _run(cases: list[dict], model_name: str, follow_up: bool,
               run_id: str) -> list[dict]:
    from api.agent3.loop import run_turn

    out = []
    for i, case in enumerate(cases):
        # Fresh thread per RUN, not per case name. Checkpoints live in Neo4j
        # and outlive the process: with a fixed id, the second run of this
        # suite resumed the first run's conversation and answered "I already
        # answered this above" from history -- a correct answer, a clean
        # memory demonstration, and a worthless smoke test, because the case
        # never exercised the tools. A smoke run must start cold.
        thread = f"smoke-{run_id}-{case['id']}"
        r = await run_turn(case["question"], thread_id=thread,
                           model_name=model_name)
        problems = case["check"](r)
        # A blocking finding that survives to here means the gate saw it,
        # spent its one repair, and the model produced the same defect again.
        # That is a genuine failure of the turn, not an advisory note.
        problems += [f"[gate] {p}" for p in (r.gate_findings or {}).get("blocking", [])]
        row = {
            "id": case["id"], "question": case["question"],
            "why": case["why"], "answer": r.answer,
            "tool_calls": r.tool_calls, "model_calls": r.model_calls,
            "skills": r.skills_loaded, "seconds": r.seconds,
            "auction_ids": r.auction_ids[:5], "usage": r.usage,
            "gate_repairs": r.gate_repairs,
            "gate_repaired": r.gate_repaired,
            "gate_advisory": (r.gate_findings or {}).get("advisory", []),
            "problems": problems,
            "status": "PASS" if not problems else "FAIL",
        }
        out.append(row)
        _print_row(row)

        if follow_up and i == 0:
            r2 = await run_turn(FOLLOW_UP, thread_id=thread,
                                model_name=model_name)
            cached = (r2.usage or {}).get("cached_input_tokens")
            total_in = (r2.usage or {}).get("input_tokens")
            share = (round(100 * cached / total_in) if cached and total_in
                     else 0)
            row2 = {
                "id": "memory_followup", "question": FOLLOW_UP,
                "why": "same thread — proves server-side memory, and reads "
                       "the cache share the loop A/B found at zero",
                "answer": r2.answer, "tool_calls": r2.tool_calls,
                "model_calls": r2.model_calls, "skills": r2.skills_loaded,
                "seconds": r2.seconds, "auction_ids": r2.auction_ids[:5],
                "usage": r2.usage, "cache_share_pct": share,
                "problems": check_memory_worked(r2),
                "status": "PASS" if not check_memory_worked(r2) else "FAIL",
            }
            out.append(row2)
            _print_row(row2)
    return out


def _print_row(row: dict) -> None:
    mark = {"PASS": "ok  ", "FAIL": "FAIL"}[row["status"]]
    print(f"\n{mark} {row['id']}  ({row['seconds']}s, "
          f"{row['model_calls']} model / {row['tool_calls']} tool calls, "
          f"skills={row['skills'] or '-'})")
    print(f"     Q: {row['question']}")
    answer = (row["answer"] or "").replace("\n", " ")
    print(f"     A: {answer[:400]}{'...' if len(answer) > 400 else ''}")
    if row.get("usage"):
        u = row["usage"]
        print(f"     tokens: in={u.get('input_tokens')} "
              f"out={u.get('output_tokens')} "
              f"cached={u.get('cached_input_tokens')}")
    if "cache_share_pct" in row:
        print(f"     CACHE SHARE: {row['cache_share_pct']}%")
    if row.get("gate_repairs"):
        print(f"     gate: {row['gate_repairs']} repair(s) spent")
        for p in row.get("gate_repaired") or []:
            print(f"       caught: {p}")
    # Advisory findings are printed but never counted against the case. They
    # are the evidence for whether the numeric tier could ever be promoted to
    # blocking — a rate near zero on correct answers would make the case, and
    # anything else settles it the other way.
    for a in row.get("gate_advisory") or []:
        print(f"     gate advisory: {a}")
    for p in row["problems"]:
        print(f"     - {p}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", help="run one case by id")
    ap.add_argument("--model", default="flash")
    ap.add_argument("--json", dest="json_path")
    ap.add_argument("--no-follow-up", action="store_true")
    args = ap.parse_args(argv)

    cases = [c for c in CASES if not args.case or c["id"] == args.case]
    if not cases:
        print("no case matched", file=sys.stderr)
        return 2

    # Wall-clock is fine as a run id here: this is a manual smoke tool, not
    # something two copies of which race each other.
    run_id = str(int(time.time()))
    rows = asyncio.run(_run(cases, args.model, not args.no_follow_up, run_id))
    print(f"(thread prefix: smoke-{run_id})")

    scored = [r for r in rows if r["status"] != "SKIP"]
    passed = sum(1 for r in scored if r["status"] == "PASS")
    total_s = sum(r["seconds"] for r in rows)
    calls = [r["model_calls"] for r in rows]
    tin = sum((r.get("usage") or {}).get("input_tokens") or 0 for r in rows)
    tout = sum((r.get("usage") or {}).get("output_tokens") or 0 for r in rows)
    tcached = sum((r.get("usage") or {}).get("cached_input_tokens") or 0
                  for r in rows)
    cache_pct = round(100 * tcached / tin) if tin else 0

    print(f"\n{'=' * 60}")
    print(f"{passed}/{len(scored)} passed · {total_s:.1f}s total · "
          f"median {sorted(calls)[len(calls) // 2] if calls else 0} model calls/turn")
    # Summed across every model call in every turn — see loop._usage_of for
    # the two ways this has been got wrong before (re-billing history, and
    # counting only the final call).
    print(f"tokens: {tin:,} in · {tout:,} out · {tcached:,} cached "
          f"({cache_pct}% of input)")
    repairs = sum(r.get("gate_repairs") or 0 for r in rows)
    advisory = sum(len(r.get("gate_advisory") or []) for r in rows)
    print(f"gate: {repairs} repair(s) across {len(rows)} turns · "
          f"{advisory} advisory amount finding(s) — advisory findings on "
          f"correct answers are false positives, and are why that tier does "
          f"not block")

    if args.json_path:
        p = Path(args.json_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rows, indent=2, default=str))
        print(f"report written to {p}")

    return 0 if passed == len(scored) else 1


if __name__ == "__main__":
    raise SystemExit(main())
