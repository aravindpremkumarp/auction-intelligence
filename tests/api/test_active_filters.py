"""Tests for `_extract_active_filters` and the `_extract_artifacts` /
`_strip_ui_rows_from_history` plumbing — fixes for feedback items
d32d18ce (filter carry-over) and 137e1558 (LLM/UI row split).
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _search_turn(call_id: str, args: dict, total_count: int) -> tuple[ModelResponse, ModelRequest]:
    """Build the two-message pair that represents one search_auctions tool
    invocation: the model's ToolCallPart and the tool's ToolReturnPart."""
    call = ModelResponse(
        parts=[ToolCallPart(tool_name="search_auctions", tool_call_id=call_id, args=args)],
    )
    ret = ModelRequest(
        parts=[ToolReturnPart(
            tool_name="search_auctions",
            tool_call_id=call_id,
            content={"total_count": total_count, "results": []},
            timestamp=_now(),
        )],
    )
    return call, ret


# ── _extract_active_filters ────────────────────────────────────────────────

def test_active_filters_accumulate_across_turns() -> None:
    from api.chat.router import _extract_active_filters
    msgs = []
    c1, r1 = _search_turn("a", {"bank": "Canara Bank"}, 834)
    msgs.extend([c1, r1])
    c2, r2 = _search_turn("b", {"property_type": "Land", "city": "Chennai"}, 91)
    msgs.extend([c2, r2])

    filters, total = _extract_active_filters(msgs)

    assert filters == {"bank": "Canara Bank", "property_type": "Land", "city": "Chennai"}
    assert total == 91


def test_later_turn_overwrites_same_key() -> None:
    from api.chat.router import _extract_active_filters
    msgs = []
    c1, r1 = _search_turn("a", {"city": "Chennai"}, 481)
    msgs.extend([c1, r1])
    c2, r2 = _search_turn("b", {"city": "Kanchipuram"}, 120)
    msgs.extend([c2, r2])

    filters, total = _extract_active_filters(msgs)

    assert filters == {"city": "Kanchipuram"}
    assert total == 120


def test_explicit_none_clears_filter() -> None:
    from api.chat.router import _extract_active_filters
    msgs = []
    c1, r1 = _search_turn("a", {"bank": "Canara Bank", "property_type": "Land"}, 226)
    msgs.extend([c1, r1])
    # User drops the bank scope — model signals this by passing bank=None.
    c2, r2 = _search_turn("b", {"bank": None, "property_type": "Land"}, 996)
    msgs.extend([c2, r2])

    filters, _ = _extract_active_filters(msgs)

    assert "bank" not in filters
    assert filters["property_type"] == "Land"


def test_non_scope_keys_are_dropped() -> None:
    """limit / order_by / aggregations are per-call, not scope."""
    from api.chat.router import _extract_active_filters
    c, r = _search_turn("a", {
        "bank": "Canara Bank",
        "limit": 5,
        "order_by": "price_asc",
        "aggregations": ["min", "max"],
        "aggregate_field": "reserve_price_num",
    }, 5)

    filters, _ = _extract_active_filters([c, r])

    assert filters == {"bank": "Canara Bank"}


def test_non_search_tools_ignored() -> None:
    from api.chat.router import _extract_active_filters
    # A list_distinct call shouldn't pollute the search scope.
    c = ModelResponse(parts=[
        ToolCallPart(tool_name="list_distinct", tool_call_id="x", args={"field": "bank"}),
    ])
    r = ModelRequest(parts=[
        ToolReturnPart(tool_name="list_distinct", tool_call_id="x", content={}, timestamp=_now()),
    ])

    filters, total = _extract_active_filters([c, r])

    assert filters == {}
    assert total is None


# ── _extract_artifacts + ui_rows split ─────────────────────────────────────

def test_extract_artifacts_moves_ui_results_to_ui_rows() -> None:
    from api.chat.router import _extract_artifacts
    call = ModelResponse(parts=[
        ToolCallPart(
            tool_name="search_auctions",
            tool_call_id="a",
            args={"city": "Chennai", "property_type": "Land", "limit": 20},
        ),
    ])
    ret = ModelRequest(parts=[
        ToolReturnPart(
            tool_name="search_auctions",
            tool_call_id="a",
            content={
                "total_count": 91,
                "results": [{"auction_id": str(i)} for i in range(20)],
                "_ui_results": [{"auction_id": str(i)} for i in range(91)],
                "limit": 20,
                "returned": 20,
            },
            timestamp=_now(),
        ),
    ])

    artifacts = _extract_artifacts([call, ret])

    assert len(artifacts) == 1
    art = artifacts[0]
    # UI gets the 91-row side-channel.
    assert art.ui_rows is not None
    assert len(art.ui_rows) == 91
    # LLM-facing `result` must not leak `_ui_results`.
    assert "_ui_results" not in art.result
    # The 20-row model-visible slice is preserved.
    assert len(art.result["results"]) == 20


def test_extract_artifacts_ui_rows_none_when_no_overflow() -> None:
    from api.chat.router import _extract_artifacts
    call = ModelResponse(parts=[
        ToolCallPart(tool_name="search_auctions", tool_call_id="a", args={"city": "Chennai"}),
    ])
    ret = ModelRequest(parts=[
        ToolReturnPart(
            tool_name="search_auctions",
            tool_call_id="a",
            content={"total_count": 3, "results": [{"auction_id": "1"}], "limit": 20},
            timestamp=_now(),
        ),
    ])

    artifacts = _extract_artifacts([call, ret])

    assert artifacts[0].ui_rows is None


def test_strip_ui_rows_from_history() -> None:
    """The dumped history echoed back to the client must not carry
    `_ui_results` — otherwise it re-enters the LLM's context next turn."""
    from api.chat.router import _strip_ui_rows_from_history
    history = [
        {
            "parts": [
                {
                    "part_kind": "tool-return",
                    "tool_name": "search_auctions",
                    "content": {
                        "total_count": 91,
                        "results": [1, 2, 3],
                        "_ui_results": list(range(91)),
                    },
                },
            ],
        },
        {
            "parts": [
                {"part_kind": "text", "content": "hello"},
            ],
        },
    ]

    cleaned = _strip_ui_rows_from_history(history)

    assert "_ui_results" not in cleaned[0]["parts"][0]["content"]
    assert cleaned[0]["parts"][0]["content"]["results"] == [1, 2, 3]
    # Non-tool-return parts are untouched.
    assert cleaned[1]["parts"][0]["content"] == "hello"


def test_strip_dynamic_system_prompts_from_history() -> None:
    """Old stored histories still carry SystemPromptParts with a
    `dynamic_ref` pointing at functions we migrated to `@agent.instructions`.
    The strip helper drops those orphan refs so pydantic-ai doesn't
    re-emit stale 'Active scope' text on the next turn."""
    from api.chat.router import _strip_dynamic_system_prompts_from_history
    history = [
        {
            "parts": [
                # Static system prompt — must survive.
                {"part_kind": "system-prompt", "content": "You are an AI..."},
                # Dynamic prior_search ref — must be dropped.
                {
                    "part_kind": "system-prompt",
                    "content": "Active search scope narrowed across prior turns: ...",
                    "dynamic_ref": "Agent.system_prompt.<locals>.inject_prior_search",
                },
                # Dynamic mode_overlay ref — must be dropped (even if empty).
                {
                    "part_kind": "system-prompt",
                    "content": "",
                    "dynamic_ref": "Agent.system_prompt.<locals>.inject_mode_overlay",
                },
                # A real user message in the same ModelRequest — keeps.
                {"part_kind": "user-prompt", "content": "show me 5 cheap ones"},
            ],
        },
        {
            "parts": [
                # Tool returns must pass through untouched.
                {"part_kind": "tool-return", "tool_name": "search_auctions", "content": {"x": 1}},
            ],
        },
    ]

    cleaned = _strip_dynamic_system_prompts_from_history(history)

    msg0_kinds = [p["part_kind"] for p in cleaned[0]["parts"]]
    msg0_contents = [p.get("content") for p in cleaned[0]["parts"]]
    # Static system prompt + user prompt survived; both dynamic_ref system
    # prompts are gone.
    assert msg0_kinds == ["system-prompt", "user-prompt"]
    assert "You are an AI..." in msg0_contents
    assert "show me 5 cheap ones" in msg0_contents
    # No remaining part should carry a dynamic_ref.
    for part in cleaned[0]["parts"]:
        assert "dynamic_ref" not in part or not part.get("dynamic_ref")
    # Other message kinds (tool-return) are untouched.
    assert cleaned[1]["parts"][0]["part_kind"] == "tool-return"
    assert cleaned[1]["parts"][0]["content"] == {"x": 1}


def test_strip_dynamic_system_prompts_idempotent_when_no_refs() -> None:
    """Histories that never went through the old dynamic-system-prompt era
    must be passed through unchanged."""
    from api.chat.router import _strip_dynamic_system_prompts_from_history
    history = [
        {
            "parts": [
                {"part_kind": "system-prompt", "content": "You are an AI..."},
                {"part_kind": "user-prompt", "content": "hello"},
            ],
        },
    ]
    cleaned = _strip_dynamic_system_prompts_from_history(history)
    assert cleaned == history


def test_new_session_filters_are_carried() -> None:
    """Regression: the carry set went stale when search_auctions grew new
    filters (borrower / EMD / platform / is_reauction / auction_type /
    branch_name / deadline window) — scope in those args was silently
    dropped from the "Active search scope" block on follow-up turns."""
    from api.chat.router import _extract_active_filters
    msgs = []
    c1, r1 = _search_turn("a", {
        "borrower": "Kumar",
        "min_emd": 50_000,
        "service_provider": "BAANKNET",
        "is_reauction": True,
        "auction_type": "DRT Auction",
        "branch_name": "Anna Nagar Branch",
        "deadline_within_days": 7,
    }, 12)
    msgs.extend([c1, r1])

    filters, total = _extract_active_filters(msgs)

    assert filters == {
        "borrower": "Kumar",
        "min_emd": 50_000,
        "service_provider": "BAANKNET",
        "is_reauction": True,
        "auction_type": "DRT Auction",
        "branch_name": "Anna Nagar Branch",
        "deadline_within_days": 7,
    }
    assert total == 12


def test_is_reauction_false_is_carried_not_dropped() -> None:
    """is_reauction=False ("fresh listings") is real scope — the falsy value
    must not be confused with the explicit-None clear signal."""
    from api.chat.router import _extract_active_filters
    msgs = []
    c1, r1 = _search_turn("a", {"is_reauction": False, "city": "Chennai"}, 900)
    msgs.extend([c1, r1])

    filters, _ = _extract_active_filters(msgs)

    assert filters["is_reauction"] is False
    assert filters["city"] == "Chennai"


def test_output_shape_args_never_carried() -> None:
    """limit / order_by / group_by / aggregations / include_past shape one
    call; they are not conversation scope."""
    from api.chat.router import _extract_active_filters
    msgs = []
    c1, r1 = _search_turn("a", {
        "city": "Chennai", "limit": 5, "order_by": "price_asc",
        "group_by": "bank", "aggregations": ["avg"], "include_past": True,
    }, 42)
    msgs.extend([c1, r1])

    filters, _ = _extract_active_filters(msgs)

    assert filters == {"city": "Chennai"}
