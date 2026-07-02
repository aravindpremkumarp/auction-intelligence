"""
api/tool_returns.py
-------------------
Pure helpers for shaping agent tool return values. Kept separate from
`api/agent.py` (which builds the live OpenRouter agent and is stubbed out by
the test conftest) so this logic can be unit-tested directly — same pattern
as `api/model_selection.py`.
"""
from __future__ import annotations

from pydantic_ai.messages import ToolReturn


def split_ui_overflow(result: dict) -> ToolReturn | dict:
    """Move the `_ui_results` UI-only overflow out of the model-visible tool
    return and onto ToolReturn metadata.

    The tools layer attaches up to `_UI_ROWS_HARD_CAP` (500) full rows under
    `_ui_results` so the matches panel can render every hit. The chat router
    already strips that key from echoed history and artifacts, but a plain
    dict return also rides in the ToolReturnPart *within the running turn* —
    every subsequent model round-trip of the same turn re-serializes it
    (observed: one search inflated a request from 38k to 109k input tokens).
    ToolReturn.metadata is never sent to the model, so the rows reach the UI
    without ever entering context.
    """
    if isinstance(result, dict) and "_ui_results" in result:
        trimmed = {k: v for k, v in result.items() if k != "_ui_results"}
        return ToolReturn(
            return_value=trimmed, metadata={"ui_rows": result["_ui_results"]}
        )
    return result
