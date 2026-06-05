"""Tests for `_trim_old_tool_results` / `_summarize_tool_return_content` —
the stale-turn tool-result trimmer that shrinks the message history echoed
back into the LLM on each /chat turn.

The trimmer operates on dumped (dict-shaped) history, mirroring
`_strip_ui_rows_from_history`, so these tests build raw dicts directly.
"""
from __future__ import annotations

import json


def _heavy_search_content(n: int = 20, total: int = 91) -> dict:
    """A search_auctions-shaped result big enough to clear the trim threshold."""
    return {
        "total_count": total,
        "returned": n,
        "limit": n,
        "results": [
            {
                "auction_id": str(i),
                "title": f"Spacious Property {i} near the main road",
                "url": f"https://example.com/auctions/{i}",
                "reserve_price_num": 500000 + i,
                "city": "Chennai",
                "area": "Adyar",
            }
            for i in range(n)
        ],
    }


def _user_msg(text: str) -> dict:
    return {"parts": [{"part_kind": "user-prompt", "content": text}]}


def _call_msg(tool: str, call_id: str) -> dict:
    return {"parts": [{"part_kind": "tool-call", "tool_name": tool, "tool_call_id": call_id}]}


def _return_msg(tool: str, call_id: str, content) -> dict:
    return {"parts": [{
        "part_kind": "tool-return",
        "tool_name": tool,
        "tool_call_id": call_id,
        "content": content,
    }]}


def _search_turn(text: str, call_id: str, content) -> list[dict]:
    return [_user_msg(text), _call_msg("search_auctions", call_id), _return_msg("search_auctions", call_id, content)]


# ── _trim_old_tool_results ─────────────────────────────────────────────────

def test_old_turn_trimmed_recent_turns_kept() -> None:
    from api.chat.router import _trim_old_tool_results
    history = (
        _search_turn("cheap chennai land", "a", _heavy_search_content())
        + _search_turn("only under 10L", "b", _heavy_search_content())
        + _search_turn("show the cheapest", "c", _heavy_search_content())
    )

    _trim_old_tool_results(history, keep_full_turns=2)

    # Turn 1 (oldest) is trimmed to a stub.
    old = history[2]["parts"][0]["content"]
    assert old["_trimmed"] is True
    assert old["total_count"] == 91
    assert old["returned"] == 20
    assert old["auction_ids"][:3] == ["0", "1", "2"]
    assert "results" not in old  # the heavy rows are gone

    # Turns 2 and 3 (within the keep window) keep full rows.
    assert len(history[5]["parts"][0]["content"]["results"]) == 20
    assert len(history[8]["parts"][0]["content"]["results"]) == 20


def test_small_results_below_threshold_are_left_intact() -> None:
    """A tiny aggregate result on an old turn must survive — trimming it would
    save nothing and could lose context the model still wants cheaply."""
    from api.chat.router import _trim_old_tool_results
    small = {"counts": {"SBI": 12, "Canara": 4}}
    history = (
        [_user_msg("bank spread"), _call_msg("list_distinct", "a"),
         _return_msg("list_distinct", "a", small)]
        + _search_turn("now cheap ones", "b", _heavy_search_content())
        + _search_turn("cheapest", "c", _heavy_search_content())
    )

    _trim_old_tool_results(history, keep_full_turns=2)

    # The old list_distinct result is untouched (below the char threshold).
    assert history[2]["parts"][0]["content"] == small


def test_nothing_trimmed_when_within_keep_window() -> None:
    from api.chat.router import _trim_old_tool_results
    history = (
        _search_turn("one", "a", _heavy_search_content())
        + _search_turn("two", "b", _heavy_search_content())
    )
    before = json.dumps(history)

    _trim_old_tool_results(history, keep_full_turns=2)

    assert json.dumps(history) == before  # both turns are recent → no change


def test_trim_is_idempotent() -> None:
    from api.chat.router import _trim_old_tool_results
    history = (
        _search_turn("one", "a", _heavy_search_content())
        + _search_turn("two", "b", _heavy_search_content())
        + _search_turn("three", "c", _heavy_search_content())
    )

    _trim_old_tool_results(history, keep_full_turns=2)
    once = json.dumps(history)
    _trim_old_tool_results(history, keep_full_turns=2)

    assert json.dumps(history) == once  # second pass is a no-op


def test_keep_full_turns_one_trims_all_but_current() -> None:
    from api.chat.router import _trim_old_tool_results
    history = (
        _search_turn("one", "a", _heavy_search_content())
        + _search_turn("two", "b", _heavy_search_content())
    )

    _trim_old_tool_results(history, keep_full_turns=1)

    assert history[2]["parts"][0]["content"]["_trimmed"] is True   # turn 1 trimmed
    assert "results" in history[5]["parts"][0]["content"]          # turn 2 (current) full


def test_non_tool_return_parts_untouched() -> None:
    from api.chat.router import _trim_old_tool_results
    history = (
        [_user_msg("hi"), {"parts": [{"part_kind": "text", "content": "hello there"}]}]
        + _search_turn("search", "a", _heavy_search_content())
        + _search_turn("again", "b", _heavy_search_content())
        + _search_turn("more", "c", _heavy_search_content())
    )

    _trim_old_tool_results(history, keep_full_turns=2)

    assert history[1]["parts"][0]["content"] == "hello there"


# ── _summarize_tool_return_content ─────────────────────────────────────────

def test_summarize_list_shaped_content() -> None:
    from api.chat.router import _summarize_tool_return_content
    rows = [{"auction_id": str(i), "title": f"t{i}"} for i in range(5)]
    stub = _summarize_tool_return_content(rows)
    assert stub["_trimmed"] is True
    assert stub["returned"] == 5
    assert stub["auction_ids"] == ["0", "1", "2", "3", "4"]


def test_summarize_single_detail_keeps_id() -> None:
    from api.chat.router import _summarize_tool_return_content
    detail = {"auction_id": "777", "title": "x", "documents": [1, 2, 3]}
    stub = _summarize_tool_return_content(detail)
    assert stub["_trimmed"] is True
    assert stub["auction_id"] == "777"
    assert "documents" not in stub


def test_summarize_scalar_passthrough() -> None:
    from api.chat.router import _summarize_tool_return_content
    assert _summarize_tool_return_content("just text") == "just text"
    assert _summarize_tool_return_content(42) == 42
