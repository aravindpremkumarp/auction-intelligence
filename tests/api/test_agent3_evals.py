"""Tests for the agent3 eval harness itself.

An eval suite that cannot fail is worse than none — it reports green while
the thing it guards rots. These drive the checks and invariants with
handcrafted payloads to prove each one actually catches its violation.
"""
from __future__ import annotations

from evals import agent3_cases as C
from evals.agent3_cases import ALL_CASES, GATES


def test_every_case_has_a_unique_id_and_a_known_suite():
    ids = [c.id for c in ALL_CASES]
    assert len(ids) == len(set(ids))
    assert {c.suite for c in ALL_CASES} <= set(GATES)


def test_every_case_names_a_real_tool():
    assert {c.tool for c in ALL_CASES} <= {
        "find_properties", "get_property", "find_by_identifier",
        "search_notices", "benchmark_price", "reauction_history"}


def test_scope_honesty_gate_is_total():
    """A scope violation is a confidently wrong answer, not a miss. 90% would
    mean shipping one in ten."""
    assert GATES["scope_honesty"] == 1.00


# ── the invariant actually catches things ────────────────────────────────

def test_invariant_catches_a_per_property_size_on_a_multi_lot_notice():
    bad = {"rows": [{"auction_id": "X", "notice_lot_count": 4, "area_sqft": 900.0}]}
    problems = C.scope_invariant(bad)
    assert problems and "4-lot notice" in problems[0]


def test_invariant_catches_a_lot_scope_tag_on_a_multi_lot_notice():
    bad = {"rows": [{"auction_id": "X", "notice_lot_count": 3,
                     "area_sqft_scope": "lot"}]}
    assert C.scope_invariant(bad)


def test_invariant_catches_a_flat_property_block_on_a_multi_lot_notice():
    bad = {"properties": [{"auction_id": "X", "notice_lot_count": 2,
                           "property": {"headline_sqft": 700.0},
                           "scope_note": "note"}]}
    assert C.scope_invariant(bad)


def test_invariant_catches_a_multi_lot_notice_with_no_scope_note():
    bad = {"properties": [{"auction_id": "X", "notice_lot_count": 2}]}
    problems = C.scope_invariant(bad)
    assert any("no scope_note" in p for p in problems)


def test_invariant_passes_a_correct_single_lot_row():
    good = {"rows": [{"auction_id": "X", "notice_lot_count": 1,
                      "area_sqft": 714.0, "area_sqft_scope": "lot"}]}
    assert C.scope_invariant(good) == []


def test_invariant_catches_a_lot_scoped_identifier_match_on_a_multi_lot_notice():
    bad = {"matches": [{"identifier_kind": "survey_new", "identifier_value": "1",
                        "listings": [{"auction_id": "X", "notice_lot_count": 3,
                                     "scope": "lot"}]}]}
    problems = C.scope_invariant(bad)
    assert problems and "3-lot notice" in problems[0]


def test_invariant_catches_a_multi_lot_identifier_match_with_no_scope_note():
    bad = {"matches": [{"identifier_kind": "survey_new", "identifier_value": "1",
                        "listings": [{"auction_id": "X", "notice_lot_count": 2,
                                     "scope": "notice"}]}]}
    assert C.scope_invariant(bad)


def test_invariant_catches_a_lot_scoped_notice_search_hit_on_a_multi_lot_notice():
    bad = {"results": [{"auction_id": "X", "notice_lot_count": 4, "scope": "lot"}]}
    problems = C.scope_invariant(bad)
    assert problems and "notice-search hit" in problems[0]


def test_invariant_passes_a_correct_identifier_match():
    good = {"matches": [{"identifier_kind": "survey_new", "identifier_value": "1",
                         "listings": [{"auction_id": "X", "notice_lot_count": 2,
                                      "scope": "notice", "scope_note": "n"}]}]}
    assert C.scope_invariant(good) == []


def test_invariant_passes_a_correct_notice_search_hit():
    good = {"results": [{"auction_id": "X", "notice_lot_count": 1, "scope": "lot"}]}
    assert C.scope_invariant(good) == []


def test_invariant_passes_a_correct_multi_lot_row():
    good = {"rows": [{"auction_id": "X", "notice_lot_count": 2,
                      "notice_area_sqft_range": [100.0, 900.0],
                      "area_sqft_scope": "notice"}]}
    assert C.scope_invariant(good) == []


def test_invented_capability_invariant_catches_a_sold_price():
    """Auction.outcome is only ever 'unsold'. A sold_price key anywhere means
    a field that cannot have come from this graph."""
    bad = {"rows": [{"auction_id": "X", "sold_price": 5000000}]}
    problems = C.no_invented_capability(bad)
    assert problems and "sold_price" in problems[0]


def test_invented_capability_invariant_looks_inside_nested_payloads():
    bad = {"properties": [{"auction_id": "X",
                           "property": {"auctions": [{"winning_bid": 1}]}}]}
    assert C.no_invented_capability(bad)


def test_invented_capability_invariant_passes_a_normal_payload():
    ok = {"rows": [{"auction_id": "X", "reserve_price": 4000000,
                    "notice_lot_count": 1}]}
    assert C.no_invented_capability(ok) == []


# ── the runner reports honestly ──────────────────────────────────────────

def test_runner_marks_a_failing_check_as_fail(monkeypatch):
    from evals import run_agent3

    case = C.Case(id="t", suite="gaps", question="q", tool="find_properties",
                  args={}, check=lambda r: ["boom"])
    monkeypatch.setattr(run_agent3, "_call", lambda c: {"rows": []})
    out = run_agent3.run_case(case)
    assert out["status"] == "FAIL" and out["problems"] == ["boom"]


def test_runner_fails_a_case_that_passes_its_check_but_breaks_an_invariant(monkeypatch):
    """The point of the invariants: a case nobody wrote a scope assertion for
    still cannot emit a scope violation."""
    from evals import run_agent3

    case = C.Case(id="t", suite="capability", question="q",
                  tool="find_properties", args={}, check=lambda r: [])
    monkeypatch.setattr(run_agent3, "_call", lambda c: {
        "rows": [{"auction_id": "X", "notice_lot_count": 5, "area_sqft": 1.0}]})
    out = run_agent3.run_case(case)
    assert out["status"] == "FAIL"
    assert any(p.startswith("[invariant]") for p in out["problems"])


def test_runner_skips_rather_than_fails_when_a_fixture_is_gone(monkeypatch):
    from evals import run_agent3

    case = C.Case(id="t", suite="lot_facts", question="q", tool="get_property",
                  args={"auction_ids": "GONE"}, check=lambda r: ["boom"],
                  fixture="GONE")
    monkeypatch.setattr(run_agent3, "_call",
                        lambda c: {"properties": [], "not_found": ["GONE"]})
    out = run_agent3.run_case(case)
    assert out["status"] == "SKIP"


# ── the history cases can actually fail ──────────────────────────────────
#
# The reason this section exists: the ONE memory case that preceded it could
# not fail. It followed a count question ("how many in Coimbatore?" — 35) and
# then asked "which bank is conducting that one?", where "that one" has no
# referent. Across two runs the model once asked which was meant and once
# picked arbitrarily; both passed, because the only assertion was the absence
# of amnesia phrases. Every check below is driven with a wrong answer to
# prove it says so.

def _turn(answer: str, *, tool_calls: int = 1):
    from api.agent3.loop import TurnResult

    return TurnResult(answer=answer, tool_calls=tool_calls, model_calls=2)


def test_every_history_case_is_actually_multi_turn():
    """A one-turn 'history' case tests nothing about history."""
    from evals import smoke_agent3 as S

    history = [c for c in S.CASES if c["id"].startswith("history")]
    assert history, "no history cases registered"
    for case in history:
        assert len(S._turns_of(case)) >= 2, f"{case['id']} is a single turn"


def test_single_turn_cases_still_work_unchanged():
    """The eight original cases keep their flat shape — adding history
    coverage must not require rewriting them."""
    from evals import smoke_agent3 as S

    simple = next(c for c in S.CASES if c["id"] == "simple_filter")
    turns = S._turns_of(simple)
    assert len(turns) == 1 and turns[0]["question"] == simple["question"]


def test_referent_check_fails_when_the_bank_is_missing():
    """The old case's exact failure mode: a fluent reply that never resolves
    what 'it' referred to."""
    from evals import smoke_agent3 as S

    vague = _turn("Could you clarify which auction you mean?")
    assert S.check_resolves_the_referent(vague)


def test_referent_check_passes_when_the_bank_is_named():
    from evals import smoke_agent3 as S

    assert S.check_resolves_the_referent(
        _turn("It is conducted by Canara Bank.")) == []


def test_carryover_check_catches_a_dropped_city():
    """The dangerous one: drop the city and the agent returns plausible
    results for the whole state with nothing looking broken."""
    from evals import smoke_agent3 as S

    widened = _turn("Here are 214 residential auctions under 50 lakhs.")
    problems = S.check_carries_the_filter(widened)
    assert problems and "Coimbatore" in problems[0]


def test_carryover_check_passes_when_the_city_survives():
    from evals import smoke_agent3 as S

    assert S.check_carries_the_filter(
        _turn("16 of the Coimbatore listings are under 50 lakhs.")) == []


def test_correction_check_catches_a_restated_old_answer():
    """History must be revisable. An agent that treats turn 1 as fixed is as
    wrong as one that forgets it."""
    from evals import smoke_agent3 as S

    ignored = _turn("There are 27 residential auctions in Coimbatore.")
    problems = S.check_accepts_the_correction(ignored)
    assert any("Chennai" in p for p in problems)


def test_correction_check_requires_a_fresh_query():
    """Switching city without calling a tool means the numbers are the old
    city's, restated."""
    from evals import smoke_agent3 as S

    no_tool = _turn("Chennai has plenty of listings.", tool_calls=0)
    problems = S.check_accepts_the_correction(no_tool)
    assert any("re-quer" in p for p in problems)


def test_correction_check_passes_on_a_real_switch():
    from evals import smoke_agent3 as S

    assert S.check_accepts_the_correction(
        _turn("In Chennai there are 32 upcoming residential auctions.")) == []


def test_runner_reports_a_crash_instead_of_swallowing_it(monkeypatch):
    from evals import run_agent3

    def boom(case):
        raise RuntimeError("neo4j is down")

    case = C.Case(id="t", suite="gaps", question="q", tool="get_property",
                  args={}, check=lambda r: [])
    monkeypatch.setattr(run_agent3, "_call", boom)
    out = run_agent3.run_case(case)
    assert out["status"] == "ERROR" and "neo4j is down" in out["problems"][0]
