"""
api/agent3/artifacts.py
-----------------------
Turns a finished turn into the `artifacts` list `web/app.js` already renders.

The shape is **deliberately byte-identical to v1's `ToolArtifact`** — `tool`,
`args`, `result`, `ui_rows` — for the same reason `api/chat/v2/artifacts.py`
keeps it: `extractResultsFromArtifacts`, `collectAuctionIds` and the rest of
the matches-panel path in the frontend then work with no changes. Keeping the
agent clean-slate was never a licence to make the UI rewrite itself.

**Where this differs from v1 and v2, and why it is simpler.** Both of those
have to *infer* which listings the panel should show, by parsing the answer
for cited ids and comparing them against every tool return
(`api/chat/panel.py::panel_sync_ids`). They infer because their tools hand the
model and the panel the same payload, so nothing recorded which rows were
"the result".

agent3 already split those: `find_properties` writes the full match set to a
`ToolSink` that the model never sees. So the panel's rows are known exactly,
not guessed, and no heuristic is involved. `panel_sync_ids` exists to solve a
problem this design does not have.

The fallback below is the one case the sink cannot cover: a turn that answered
about specific listings without running a search — `get_property`,
`benchmark_price`, `reauction_history` all reach the graph by id and put
nothing in the sink. There the cited ids ARE the panel, so they are fetched.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("api.agent3.artifacts")

#: A six-digit portal id in prose. Same shape the answer gate matches — see
#: `api/agent3/gates.py::ID_LIKE` for why six digits with these lookarounds is
#: safe against prices and longer numbers.
_ID_IN_ANSWER = re.compile(r"(?<!\d)(?<!\d,)(?<!\d\.)(\d{6})(?!\d)(?!,\d)(?!\.\d)")

#: Cap on ids fetched for the fallback. A turn citing more listings than this
#: is a search, and a search fills the sink.
MAX_FALLBACK_IDS = 25


def cited_ids(answer: str) -> list[str]:
    """Six-digit ids the answer names, in order, deduplicated."""
    out: list[str] = []
    for m in _ID_IN_ANSWER.finditer(answer or ""):
        token = m.group(1)
        if token not in out:
            out.append(token)
    return out


async def build_artifacts(result, *, panel_before: list[str] | None = None
                          ) -> list[dict[str, Any]]:
    """The artifacts for one turn.

    Best-effort throughout: the panel is cosmetic, and a failure here must
    never fail a turn that already has a good answer. That rule is inherited
    from v2's builder and it has earned its place — a panel bug taking down a
    correct answer is the worst possible trade.
    """
    try:
        if result.panel_rows:
            return [_search_artifact(result)]
        return await _fallback_artifacts(result, panel_before)
    except Exception:  # noqa: BLE001 - cosmetic, never fatal
        logger.exception("agent3 artifact build failed — leaving panel as-is")
        return []


def _search_artifact(result) -> dict[str, Any]:
    """The sink's rows, shaped as the search artifact the frontend renders.

    `result` carries every match the search found, up to `PANEL_ROW_CAP` —
    not the ten-row sample the model was shown. That asymmetry is the whole
    point of the sink: the panel stays complete while the transcript stays
    small.
    """
    return {
        "tool": "find_properties",
        "args": {"synthetic": True, "source": "sink"},
        "result": {"rows": result.panel_rows,
                   "total_count": len(result.panel_rows)},
        "ui_rows": result.panel_rows,
    }


async def _fallback_artifacts(result, panel_before: list[str] | None
                              ) -> list[dict[str, Any]]:
    """Panel rows for a turn that answered by id rather than by search.

    Skipped when the answer cites exactly what the panel is already showing —
    re-sending an identical set would make the panel flicker for no change,
    which is the behaviour `panel_sync_ids` guards in v1/v2.
    """
    import asyncio

    ids = cited_ids(result.answer or "")[:MAX_FALLBACK_IDS]
    if not ids:
        return []
    if panel_before and set(ids) == set(panel_before):
        return []

    from api.tools import cypher_tools as cypher_T

    rows = await asyncio.to_thread(cypher_T.get_auctions_by_ids, ids)
    return [{
        "tool": "select_properties",
        "args": {"auction_ids": ids, "synthetic": True},
        "result": rows,
        "ui_rows": None,
    }]
