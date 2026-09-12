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
    pairs = ll.find_pairs([_rec("841207", "eauctionsindia"), _rec("bn-1", "baanknet"), _rec("bn-2", "baanknet"),
                           _rec("be-1", "bankeauctions", bank="Indian Bank")])
    assert {tuple(sorted((p.a_id, p.b_id))) for p in pairs} == {("841207", "bn-1"), ("841207", "bn-2")}
    assert all(p.method == "borrower" and p.confidence == "PROBABLE" for p in pairs)
    rows = ll.pair_rows(pairs + [Pair("x", "y", "baanknet", "baanknet", "borrower", "PROBABLE")])
    assert len(rows) == 2 and {"a_id", "b_id", "method", "confidence", "evidence"} == set(rows[0])
    assert "PROBABLE  borrower     2" in ll.summarize(pairs)


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
