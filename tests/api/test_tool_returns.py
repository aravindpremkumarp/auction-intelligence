"""
tests/api/test_tool_returns.py
------------------------------
The `_ui_results` overflow (up to 500 full rows for the matches panel) must
never enter the model's context. The router already stripped it from echoed
history and artifacts, but a plain dict tool return also rides in the
ToolReturnPart *within the running turn* — every subsequent model round-trip
of the same turn re-serialized it (observed in Logfire: one search inflated
a request from 38k to 109k input tokens). `split_ui_overflow` moves the rows
onto ToolReturn metadata, which pydantic-ai never sends to the model.

Covers the three surfaces:
  1. `split_ui_overflow` — the agent-side split itself.
  2. `_extract_artifacts` — the router recovers `ui_rows` from metadata.
  3. `_strip_ui_rows_from_history` — metadata rows don't bloat the dumped
     history the client stores and echoes back.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturn,
    ToolReturnPart,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── split_ui_overflow ──────────────────────────────────────────────────────

def test_split_moves_ui_results_to_metadata() -> None:
    from api.tool_returns import split_ui_overflow

    result = {
        "total_count": 300,
        "returned": 10,
        "limit": 10,
        "results": [{"auction_id": str(i)} for i in range(10)],
        "_ui_results": [{"auction_id": str(i)} for i in range(300)],
    }
    out = split_ui_overflow(result)

    assert isinstance(out, ToolReturn)
    # Model-visible value: no `_ui_results`, everything else intact.
    assert "_ui_results" not in out.return_value
    assert out.return_value["total_count"] == 300
    assert len(out.return_value["results"]) == 10
    # UI rows ride on metadata (never serialized for the model).
    assert len(out.metadata["ui_rows"]) == 300


def test_split_passes_through_without_overflow() -> None:
    from api.tool_returns import split_ui_overflow

    result = {"total_count": 3, "returned": 3, "limit": 10, "results": []}
    assert split_ui_overflow(result) is result


def test_split_does_not_mutate_original() -> None:
    from api.tool_returns import split_ui_overflow

    result = {"total_count": 1, "results": [], "_ui_results": [{"auction_id": "1"}]}
    split_ui_overflow(result)
    # The tools-layer dict is shared with callers/telemetry — must stay intact.
    assert "_ui_results" in result


# ── _extract_artifacts reads metadata ──────────────────────────────────────

def _turn_with_metadata(ui_rows: list | None) -> list:
    call = ModelResponse(parts=[
        ToolCallPart(tool_name="search_auctions", tool_call_id="c1", args={"city": "Chennai"}),
    ])
    ret = ModelRequest(parts=[
        ToolReturnPart(
            tool_name="search_auctions",
            tool_call_id="c1",
            content={"total_count": 91, "returned": 2, "results": [{"auction_id": "1"}]},
            metadata={"ui_rows": ui_rows} if ui_rows is not None else None,
            timestamp=_now(),
        ),
    ])
    return [call, ret]


def test_extract_artifacts_recovers_ui_rows_from_metadata() -> None:
    from api.chat.router import _extract_artifacts

    msgs = _turn_with_metadata([{"auction_id": str(i)} for i in range(91)])
    artifacts = _extract_artifacts(msgs)

    assert len(artifacts) == 1
    art = artifacts[0]
    assert art.ui_rows is not None and len(art.ui_rows) == 91
    assert "_ui_results" not in art.result


def test_extract_artifacts_ui_rows_none_without_metadata() -> None:
    from api.chat.router import _extract_artifacts

    artifacts = _extract_artifacts(_turn_with_metadata(None))
    assert artifacts[0].ui_rows is None


# ── _strip_ui_rows_from_history strips metadata ────────────────────────────

def test_strip_history_removes_metadata_ui_rows() -> None:
    from api.chat.router import _strip_ui_rows_from_history

    history = [{
        "parts": [{
            "part_kind": "tool-return",
            "tool_name": "search_auctions",
            "content": {"total_count": 91, "results": []},
            "metadata": {"ui_rows": [{"auction_id": str(i)} for i in range(91)]},
        }],
    }]
    cleaned = _strip_ui_rows_from_history(history)
    part = cleaned[0]["parts"][0]
    assert part["metadata"] is None  # emptied → nulled, not left as {}
    assert part["content"] == {"total_count": 91, "results": []}


def test_strip_history_keeps_unrelated_metadata() -> None:
    from api.chat.router import _strip_ui_rows_from_history

    history = [{
        "parts": [{
            "part_kind": "tool-return",
            "content": {},
            "metadata": {"ui_rows": [1, 2], "other": "keep"},
        }],
    }]
    cleaned = _strip_ui_rows_from_history(history)
    assert cleaned[0]["parts"][0]["metadata"] == {"other": "keep"}


def test_strip_history_still_handles_legacy_content_rows() -> None:
    """Stored histories from before the metadata split still carry
    `_ui_results` inside content — the legacy path must keep working."""
    from api.chat.router import _strip_ui_rows_from_history

    history = [{
        "parts": [{
            "part_kind": "tool-return",
            "content": {"total_count": 5, "_ui_results": list(range(5))},
        }],
    }]
    cleaned = _strip_ui_rows_from_history(history)
    assert "_ui_results" not in cleaned[0]["parts"][0]["content"]


# ── the agent wrapper actually applies the split ───────────────────────────

def test_agent_search_tool_routes_through_split() -> None:
    """The whole fix hinges on one call in the `search_auctions` tool wrapper.
    conftest replaces api.agent with a stub, so assert via AST (same approach
    as test_prompt_budget) that the wrapper returns through
    `split_ui_overflow` — reverting that line must fail a test, not slip
    through green."""
    import ast
    from pathlib import Path

    agent_py = Path(__file__).resolve().parents[2] / "api" / "agent.py"
    mod = ast.parse(agent_py.read_text(encoding="utf-8"))
    for node in ast.walk(mod):
        if isinstance(node, ast.FunctionDef) and node.name == "search_auctions":
            assert "split_ui_overflow" in ast.unparse(node), (
                "search_auctions must return through split_ui_overflow — "
                "without it, up to 500 _ui_results rows re-enter the model's "
                "context on every same-turn round-trip"
            )
            return
    raise AssertionError("search_auctions tool not found in api/agent.py")
