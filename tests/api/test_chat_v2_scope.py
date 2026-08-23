"""
tests/api/test_chat_v2_scope.py
-------------------------------
The v2 scope object is a trust boundary: unlike v1's message_history (inert
prose the model only reads), it is merged into `search_auctions` kwargs by
code. These tests pin both halves — the sanitizer against hostile input, and
the merge/harvest rules that make narrowing work without a transcript.
"""
from __future__ import annotations

import pytest

from api.chat.scope_keys import CARRY_FORWARD_FILTER_KEYS
from api.chat.v2.scope import (
    MAX_ENTITIES_PER_DIM,
    MAX_FILTER_LIST,
    MAX_FILTER_STR,
    MAX_QUESTION_CHARS,
    harvest_entities,
    harvest_scope,
    merge_scope,
    sanitize_entities,
    sanitize_ids,
    sanitize_question,
    sanitize_scope,
)


# ── sanitize_scope: the trust boundary ──────────────────────────────────────

def test_keeps_known_filters():
    assert sanitize_scope({"city": "Chennai", "max_price": 4000000}) == {
        "city": "Chennai", "max_price": 4000000}


def test_drops_unknown_keys():
    """A key not in the shared set is filter injection — a client naming a
    `search_auctions` argument the product never meant to expose."""
    out = sanitize_scope({"city": "Chennai", "limit": 500, "__class__": "x",
                          "order_by": "price_desc"})
    assert out == {"city": "Chennai"}


def test_drops_unusable_value_types():
    assert sanitize_scope({"city": {"$ne": None}}) == {}
    assert sanitize_scope({"city": object()}) == {}


def test_caps_string_length():
    out = sanitize_scope({"city": "x" * (MAX_FILTER_STR + 500)})
    assert len(out["city"]) == MAX_FILTER_STR


def test_caps_list_length():
    out = sanitize_scope({"city": [f"c{i}" for i in range(MAX_FILTER_LIST + 50)]})
    assert len(out["city"]) == MAX_FILTER_LIST


def test_list_of_junk_becomes_dropped_not_empty():
    """An all-junk list must drop the filter, not pass an empty list through —
    `city=[]` would silently match nothing."""
    assert sanitize_scope({"city": [None, {}, []]}) == {}


def test_explicit_none_survives():
    """None is meaningful — 'this filter was dropped' — and is not the same as
    a value the sanitizer rejected."""
    assert sanitize_scope({"city": None}) == {"city": None}


def test_bool_is_not_coerced_to_int():
    assert sanitize_scope({"is_reauction": True}) == {"is_reauction": True}


def test_non_dict_input():
    assert sanitize_scope(None) == {}
    assert sanitize_scope("city=Chennai") == {}
    assert sanitize_scope([("city", "Chennai")]) == {}


def test_whitespace_only_string_dropped():
    assert sanitize_scope({"city": "   "}) == {}


# ── sanitize_ids ────────────────────────────────────────────────────────────

def test_ids_preserve_order_and_dedupe():
    assert sanitize_ids(["3", "1", "3", "2"]) == ["3", "1", "2"]


def test_ids_bounded():
    assert len(sanitize_ids([str(i) for i in range(500)])) == 25


def test_ids_reject_junk():
    assert sanitize_ids(["", None, {}, "  ", "ok"]) == ["ok"]


# ── merge_scope: deterministic carry-forward ────────────────────────────────

def test_carry_forward_silent_keys():
    """The model expresses only changes; code carries the rest. This is what
    makes narrowing work without the model restating the city every turn."""
    merged = merge_scope({"city": "Chennai"}, {"max_price": 4000000})
    assert merged == {"city": "Chennai", "max_price": 4000000}


def test_new_value_overrides():
    merged = merge_scope({"city": "Chennai"}, {"city": "Coimbatore"})
    assert merged["city"] == "Coimbatore"


def test_explicit_none_drops_the_filter():
    merged = merge_scope({"city": "Chennai", "max_price": 100}, {"max_price": None})
    assert merged == {"city": "Chennai"}


def test_non_scope_args_pass_through_untouched():
    merged = merge_scope({"city": "Chennai"}, {"group_by": "bank", "order_by": "price_asc"})
    assert merged["group_by"] == "bank"
    assert merged["order_by"] == "price_asc"
    assert merged["city"] == "Chennai"


def test_carried_none_is_not_re_sent():
    """A dropped filter must not reappear as `key=None` in the next call —
    that would read as an explicit drop rather than an absence."""
    merged = merge_scope({"city": None, "max_price": 100}, {})
    assert merged == {"max_price": 100}


# ── harvest_scope ───────────────────────────────────────────────────────────

def _search(args, result):
    return {"tool": "search_auctions", "args": args, "result": result}


def test_harvest_reads_executed_args_not_the_plan():
    executed = [_search({"city": "Chennai", "max_price": 4000000},
                        {"total_count": 20, "results": [{"auction_id": "837057"}]})]
    filters, total, ids = harvest_scope(executed)
    assert filters == {"city": "Chennai", "max_price": 4000000}
    assert total == 20
    assert ids == ["837057"]


def test_harvest_ignores_non_search_calls():
    executed = [
        _search({"city": "Chennai"}, {"total_count": 5, "results": []}),
        {"tool": "get_auction_detail", "args": {"auction_id": "1"}, "result": {}},
        {"tool": "internet_search", "args": {"query": "sarfaesi"}, "result": {}},
    ]
    filters, total, _ = harvest_scope(executed)
    assert filters == {"city": "Chennai"}
    assert total == 5


def test_harvest_layers_on_previous():
    executed = [_search({"max_price": 4000000}, {"total_count": 20, "results": []})]
    filters, _, _ = harvest_scope(executed, previous={"city": "Chennai"})
    assert filters == {"city": "Chennai", "max_price": 4000000}


def test_harvest_honours_an_explicit_drop():
    executed = [_search({"property_type": None}, {"total_count": 90, "results": []})]
    filters, _, _ = harvest_scope(executed, previous={"property_type": "Flat",
                                                      "city": "Coimbatore"})
    assert filters == {"city": "Coimbatore"}


def test_harvest_output_is_sanitized():
    """Harvest runs the result back through the sanitizer, so a tool that
    somehow returned an off-contract arg can't seed the next turn's scope."""
    executed = [_search({"city": "Chennai"}, {"total_count": 1, "results": []})]
    filters, _, _ = harvest_scope(executed, previous={"evil": "x"})
    assert filters == {"city": "Chennai"}


def test_harvest_empty():
    assert harvest_scope([]) == ({}, None, [])


# ── the shared-source guarantee ─────────────────────────────────────────────

def test_scope_uses_the_shared_key_set():
    """v2 must filter on exactly the keys v1 carries. A fork here would mean a
    narrowing that works in one endpoint and silently doesn't in the other."""
    for key in CARRY_FORWARD_FILTER_KEYS:
        assert sanitize_scope({key: "x"}) == {key: "x"}


@pytest.mark.parametrize("key", ["limit", "order_by", "group_by", "aggregations",
                                 "include_past", "aggregate_field"])
def test_per_call_knobs_are_never_scope(key):
    """These shape one call, not the conversation. Carrying `include_past`
    would turn one retrospective question into a permanently retrospective
    conversation."""
    assert key not in CARRY_FORWARD_FILTER_KEYS
    assert sanitize_scope({key: "x"}) == {}


# ── referents: what the last answer NAMED ───────────────────────────────────
#
# The bug these pin, seen live: a turn listed nine Chennai areas, the user
# asked "which of these areas is growing fast?", and the agent replied that it
# needed to know which areas they meant. The carried ids are auction ids, so
# "these areas" pointed at nothing.

def test_entities_harvested_from_group_by_buckets():
    """In group_by mode search_auctions returns NO rows — the distribution is
    the only record of the names the user read."""
    executed = [_search(
        {"city": "Chennai", "group_by": "area"},
        {"total_count": 11, "results": [], "group_by": "area",
         "distribution": [{"value": "Ambattur", "count": 3},
                          {"value": "Padappai", "count": 1}]},
    )]
    assert harvest_entities(executed) == {"area": ["Ambattur", "Padappai"]}


def test_entities_harvested_from_result_rows():
    """An ordinary search names entities too — the synthesizer's table is
    grouped by exactly these row fields."""
    executed = [_search({"city": "Chennai"}, {"total_count": 2, "results": [
        {"auction_id": "1", "city": "Chennai", "area": "Ambattur",
         "bank": "Indian Bank"},
        {"auction_id": "2", "city": "Chennai", "area": "Tiruvarur",
         "bank": "Indian Bank"},
    ]})]
    assert harvest_entities(executed) == {
        "city": ["Chennai"],
        "area": ["Ambattur", "Tiruvarur"],
        "bank": ["Indian Bank"],
    }


def test_entities_are_capped_per_dimension():
    rows = [{"auction_id": str(i), "area": f"Area {i}"} for i in range(40)]
    out = harvest_entities([_search({}, {"total_count": 40, "results": rows})])
    assert len(out["area"]) == MAX_ENTITIES_PER_DIM


def test_entities_ignore_dimensions_that_are_not_referents():
    """Only the dimensions a follow-up refers to by name are carried. A price
    is not a referent, and carrying every row field would rebuild the
    transcript this design exists to avoid."""
    executed = [_search({}, {"total_count": 1, "results": [
        {"auction_id": "1", "area": "Ambattur", "reserve_price": 4488000,
         "title": "Plot at Ambattur"},
    ]})]
    assert harvest_entities(executed) == {"area": ["Ambattur"]}


def test_sanitize_entities_is_a_trust_boundary():
    """The client echoes these back and they land in a prompt."""
    assert sanitize_entities({"area": ["Ambattur", "", "Ambattur", 7],
                              "__class__": ["x"], "evil": ["y"],
                              "bank": "not a list"}) == {"area": ["Ambattur", "7"]}
    assert sanitize_entities("nope") == {}


def test_sanitize_question_is_bounded():
    assert sanitize_question("  which of these areas?  ") == "which of these areas?"
    assert len(sanitize_question("x" * 5000)) == MAX_QUESTION_CHARS
    assert sanitize_question({"not": "a string"}) == ""
