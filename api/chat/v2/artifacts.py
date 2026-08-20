"""
api/chat/v2/artifacts.py
------------------------
Builds the `artifacts` list for a v2 response.

The shape is **deliberately byte-identical to v1's `ToolArtifact`** — `tool`,
`args`, `result`, `ui_rows` — including the synthetic `select_properties`
entry. That is what lets `extractResultsFromArtifacts`, `setPanelSource` and
the rest of the matches-panel path in `web/app.js` work with no changes at
all, so the frontend diff for a flag flip stays confined to the
conversation-state channel.

The panel-sync decision itself is not re-implemented here: it comes from
`api/chat/panel.py::panel_sync_ids`, the same pure function v1 uses and the
same one both eval runners score against. Panel desync ("chat says 14, panel
shows 6") is exactly the bug a second implementation would reintroduce.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from api.chat.panel import panel_sync_ids
from api.tools import cypher_tools as cypher_T

logger = logging.getLogger(__name__)


def _returns(executed) -> list[tuple[str, Any]]:
    return [(call.tool, call.result) for call in executed]


async def build_artifacts(result, *, panel_before: list[str] | None = None
                          ) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = [
        {
            "tool": call.tool,
            "args": call.args,
            "result": call.result,
            # Full rows for the panel, split out by the executor so they never
            # entered a prompt.
            "ui_rows": call.ui_rows or None,
        }
        for call in result.executed
    ]
    panel = await _panel_artifact(result, panel_before)
    if panel is not None:
        artifacts.append(panel)
    return artifacts


async def _panel_artifact(result, panel_before: list[str] | None
                          ) -> dict[str, Any] | None:
    """Programmatic matches-panel sync, mirroring v1's `_synthesize_panel_artifact`.

    When the answer's cited ids re-present a subset or re-ranking of what the
    tools returned, fetch those rows and append a synthetic search-shaped
    artifact — the frontend renders the last search-shaped artifact, so it
    needs no special case. One Neo4j query, zero model round-trips.

    Best-effort: the panel is cosmetic, and a failure here must never fail a
    turn that already has a good answer.
    """
    try:
        returns = _returns(result.executed)
        ids = panel_sync_ids(result.answer or "", returns, returns, panel_before)
        if not ids:
            return None
        rows = await asyncio.to_thread(cypher_T.get_auctions_by_ids, ids)
        return {
            "tool": "select_properties",
            "args": {"auction_ids": ids, "synthetic": True},
            "result": rows,
            "ui_rows": None,
        }
    except Exception:  # noqa: BLE001 - panel sync is cosmetic, never fatal
        logger.exception("chat v2 panel sync failed — leaving panel as-is")
        return None
