"""The two writers around the spine, without a graph: link_listings turns
the matcher's pairs into edge rows (never same-source), and link_reauctions'
event pass stamps each event's place in its chain by auction date.
"""
from __future__ import annotations

from scripts import link_listings as ll
from scripts import link_reauctions as lr
from sources.match import Pair


def _rec(aid, source, bank="Indian Overseas Bank", reserve=4626500.0, day="2026-09-24", borrower="N Mariappan"):
    return {"auction_id": aid, "source": source, "bank": bank, "reserve_price_num": reserve,
            "auction_start_dt": f"{day}T11:00:00Z", "borrower": borrower, "doc_shas": [], "identifiers": [],
            "lot_bounds": [], "boundaries": {}, "description": ""}


def test_find_pairs_compares_the_whole_graph_across_sources():
    pairs = ll.find_pairs([_rec("841207", "eauctionsindia"), _rec("bn-1", "baanknet"),
                           _rec("be-1", "bankeauctions", bank="Indian Bank")])
    assert [(p.a_id, p.b_id, p.method, p.confidence) for p in pairs] == [("bn-1", "841207", "four_fields", "CONFIRMED")]
    rows = ll.pair_rows(pairs + [Pair("x", "y", "baanknet", "baanknet", "four_fields", "CONFIRMED")])
    assert len(rows) == 1 and {"a_id", "b_id", "method", "confidence", "evidence"} == set(rows[0])
    assert "CONFIRMED four_fields  1" in ll.summarize(pairs)


def test_find_pairs_never_confirms_a_batch_sale_against_one_listing():
    """bn-1 and bn-2 are one borrower's two properties; 841207 is one of them."""
    pairs = ll.find_pairs([_rec("841207", "eauctionsindia"), _rec("bn-1", "baanknet"), _rec("bn-2", "baanknet")])
    assert pairs and all(p.confidence == "PENDING" for p in pairs)


def test_chain_attempts_orders_by_date_and_stamps_previous_reserve():
    events = [
        {"auction_id": "ev-3", "auction_start_dt": "2026-09-24T11:00:00Z", "reserve": 4000000.0},
        {"auction_id": "ev-1", "auction_start_dt": "2026-03-01T11:00:00Z", "reserve": 5000000.0},
        {"auction_id": "ev-2", "auction_start_dt": "2026-06-10T11:00:00Z", "reserve": 4500000.0},
        {"auction_id": "ev-9", "auction_start_dt": None, "reserve": 1.0},
        {"auction_id": "ev-solo", "auction_start_dt": "2026-01-01T11:00:00Z", "reserve": 7.0},
    ]
    stamps = {r["id"]: r for r in lr.chain_attempts(events, [["ev-3", "ev-1", "ev-2", "ev-9"]])}
    assert (stamps["ev-1"]["attempt_no"], stamps["ev-1"]["previous_reserve"], stamps["ev-1"]["previous_event_id"]) == (1, None, None)
    assert (stamps["ev-2"]["attempt_no"], stamps["ev-2"]["previous_reserve"], stamps["ev-2"]["previous_event_id"]) == (2, 5000000.0, "ev-1")
    assert (stamps["ev-3"]["attempt_no"], stamps["ev-3"]["previous_reserve"]) == (3, 4500000.0)
    assert (stamps["ev-9"]["attempt_no"], stamps["ev-9"]["chain_size"]) == (4, 4)          # undated sorts last
    assert stamps["ev-solo"] == {"id": "ev-solo", "attempt_no": 1, "chain_size": 1, "previous_reserve": None, "previous_event_id": None}
    assert len(stamps) == 5


def test_event_pass_keeps_the_same_day_rule():
    """Two events on one day with one borrower are a batch sale, never a chain."""
    events = [
        {"auction_id": "ev-a", "borrower": "M/s Sri Vaaru Traders", "bank": "Indian Bank", "area": "Sriperumbudur",
         "city": "Chennai", "total_area": "1200 sq.ft", "auction_start_dt": "2026-09-25T11:00:00Z",
         "description": "Survey No 12/4 Sriperumbudur village land and building of the borrower with plot no 5"},
        {"auction_id": "ev-b", "borrower": "M/s Sri Vaaru Traders", "bank": "Indian Bank", "area": "Sriperumbudur",
         "city": "Chennai", "total_area": "1200 sq.ft", "auction_start_dt": "2026-09-25T11:00:00Z",
         "description": "Survey No 12/4 Sriperumbudur village land and building of the borrower with plot no 5"},
    ]
    assert lr.find_reauction_pairs(events) == []
    later = {**events[1], "auction_id": "ev-c", "auction_start_dt": "2026-11-20T11:00:00Z"}
    pairs = lr.find_reauction_pairs([events[0], later])
    assert [(a, b) for a, b, _, _ in pairs] == [("ev-a", "ev-c")]


from datetime import date  # noqa: E402

import pytest  # noqa: E402

from sources.match import Ambiguity, MatchResult  # noqa: E402


def test_safety_stop_flags_a_cluster_that_merges_two_different_prices():
    records = [_rec("841207", "eauctionsindia"), _rec("841208", "eauctionsindia", reserve=5000000.0),
               _rec("bn-1", "baanknet")]
    result = MatchResult(pairs=[Pair("bn-1", "841207", "baanknet", "eauctionsindia", "four_fields", "CONFIRMED"),
                                Pair("bn-1", "841208", "baanknet", "eauctionsindia", "decision", "CONFIRMED")],
                         ambiguous=[])
    [problem] = ll.safety_problems(result, records)
    assert "841207" in problem and "841208" in problem and "price" in problem


def test_safety_stop_flags_a_listing_both_confirmed_and_in_review_against_one_source():
    records = [_rec("841207", "eauctionsindia"), _rec("bn-1", "baanknet")]
    result = MatchResult(pairs=[Pair("bn-1", "841207", "baanknet", "eauctionsindia", "four_fields", "CONFIRMED")],
                         ambiguous=[Ambiguity("bn-1", "baanknet", "eauctionsindia", ("841207",), "batch")])
    [problem] = ll.safety_problems(result, records)
    assert "bn-1" in problem and "review" in problem


def test_a_clean_result_has_no_safety_problems():
    records = [_rec("841207", "eauctionsindia"), _rec("bn-1", "baanknet")]
    assert ll.safety_problems(ll.match(records), records) == []


def test_spot_check_is_deterministic_and_skips_decided_subjects():
    pairs = [Pair(f"bn-{i}", str(900000 + i), "baanknet", "eauctionsindia", "four_fields", "CONFIRMED") for i in range(30)]
    pairs.append(Pair("bn-99", "999", "baanknet", "eauctionsindia", "decision", "CONFIRMED"))
    result = MatchResult(pairs=pairs, ambiguous=[])
    decided = {("bn-3", "eauctionsindia")}
    first = ll.spot_check_sample(result, decided, date(2026, 9, 15))
    assert first == ll.spot_check_sample(result, decided, date(2026, 9, 15))
    assert len(first) == 10 and ("bn-3", "eauctionsindia") not in first
    assert all(subject != "bn-99" for subject, _ in first)
    assert first != ll.spot_check_sample(result, decided, date(2026, 9, 16))


def test_review_rows_carry_both_sides_and_the_snapshot():
    records = [_rec("853518", "eauctionsindia", borrower="M/s ARR Tex"), _rec("bn-359756", "baanknet", borrower="A R R TEX")]
    result = ll.match(records)
    [row] = ll.review_rows(result, records, [])
    assert (row["subject"]["auction_id"], row["other_source"], row["reason"], row["spot_check"]) == \
        ("bn-359756", "eauctionsindia", "price_only", False)
    assert [c["auction_id"] for c in row["candidates"]] == ["853518"]
    assert row["snapshot"]["borrower"] == "a r r tex"


def test_review_rows_include_spot_checked_confirmations():
    records = [_rec("841207", "eauctionsindia"), _rec("bn-1", "baanknet")]
    result = ll.match(records)
    [row] = ll.review_rows(result, records, [("bn-1", "eauctionsindia")])
    assert (row["subject"]["auction_id"], row["reason"], row["spot_check"]) == ("bn-1", "spot_check", True)
    assert [c["auction_id"] for c in row["candidates"]] == ["841207"]


def test_spot_checks_are_per_other_source():
    records = [_rec("841207", "eauctionsindia"), _rec("be-1", "bankeauctions", borrower="Mr. N Mariappan"), _rec("bn-1", "baanknet")]
    result = ll.match(records)
    keys = ll.spot_check_sample(result, set(), date(2026, 9, 15))
    assert ("bn-1", "bankeauctions") in keys and ("bn-1", "eauctionsindia") in keys
    rows = ll.review_rows(result, records, [("bn-1", "bankeauctions"), ("bn-1", "eauctionsindia")])
    assert [(r["other_source"], [c["auction_id"] for c in r["candidates"]]) for r in rows] == [
        ("bankeauctions", ["be-1"]), ("eauctionsindia", ["841207"])]


def test_run_writes_nothing_when_the_safety_stop_fires(monkeypatch):
    calls = []
    records = [_rec("841207", "eauctionsindia"), _rec("bn-1", "baanknet")]

    def fake_run_query(cypher, params=None):
        calls.append(cypher)
        if "ResolutionDecision" in cypher:
            return []
        return records

    monkeypatch.setattr(ll, "safety_problems", lambda result, recs: ["two different prices merged"])
    with pytest.raises(ll.LinkSafetyError, match="two different prices"):
        ll.run(run_query=fake_run_query)
    assert not any("DELETE" in c or "MERGE" in c for c in calls)
