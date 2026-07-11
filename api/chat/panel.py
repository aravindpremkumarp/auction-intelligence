"""
api/chat/panel.py
-----------------
Programmatic matches-panel sync. The agent used to carry a
`select_properties` TOOL whose only job was to relay auction_ids the model
had already written into its answer — a pure round-trip tax (the tool
schema rode on every LLM call, and each panel update cost an extra model
turn). The system now does that step itself:

  1. Collect every auction_id the conversation's tool results have surfaced
     (the "known" set — includes breadcrumb stubs from trimmed history and
     the client-supplied panel ids).
  2. Scan the final answer text for those ids, in first-mention order.
     Intersecting with the known set is load-bearing: auction_ids are
     6-digit numbers, so a bare regex would also match PIN codes, prices,
     and phone fragments.
  3. When the cited set differs from what this turn's artifacts would
     already put on the panel, the chat router fetches fresh rows
     (`get_auctions_by_ids`) and appends a SYNTHETIC `select_properties`
     artifact. The frontend renders the last search-shaped artifact, so it
     needs zero changes.

Everything here is pure (no I/O) so it's dependency-free unit-testable;
the router owns the one Neo4j fetch.
"""
from __future__ import annotations

import re
from typing import Any

# Tools whose results the frontend treats as panel content (mirror of
# app.js SEARCH_TOOLS/DETAIL_TOOLS; `semantic_property_search` is
# semantic_search's pre-rename name, kept for stored conversations).
_SEARCH_TOOLS = {
    "search_auctions", "semantic_search", "semantic_property_search",
    "select_properties",
}
_DETAIL_TOOLS = {"get_auction_detail"}

_ID_RE = re.compile(r"\b\d{4,10}\b")

# Bound the synthetic fetch: mirrors _BY_IDS_MAX on get_auctions_by_ids.
MAX_PANEL_SYNC_IDS = 25


def _ids_from_content(content: Any) -> list[str]:
    """auction_ids inside one tool-return payload, in row order. Handles
    search-shaped dicts ({results: [...]}), detail dicts ({auction_id}),
    trimmed-history breadcrumb stubs ({auction_ids: [...]}), and bare row
    lists."""
    out: list[str] = []
    if isinstance(content, dict):
        rows = content.get("results")
        if isinstance(rows, list):
            out.extend(str(r["auction_id"]) for r in rows
                       if isinstance(r, dict) and r.get("auction_id"))
        stub_ids = content.get("auction_ids")
        if isinstance(stub_ids, list):
            out.extend(str(i) for i in stub_ids if i)
        if content.get("auction_id"):
            out.append(str(content["auction_id"]))
    elif isinstance(content, list):
        out.extend(str(r["auction_id"]) for r in content
                   if isinstance(r, dict) and r.get("auction_id"))
    return out


def known_auction_ids(
    tool_returns: list[tuple[str, Any]],
    panel_ids: list[str] | None = None,
) -> set[str]:
    """Every auction_id this conversation has legitimately surfaced.

    `tool_returns` is [(tool_name, content), ...] across ALL messages
    (history + this turn); `panel_ids` is the client-supplied current panel
    state. Only ids in this set may be extracted from answer text."""
    known: set[str] = set(panel_ids or [])
    for _tool, content in tool_returns:
        known.update(_ids_from_content(content))
    return known


def cited_ids(answer: str, known: set[str]) -> list[str]:
    """auction_ids mentioned in the answer, first-mention order, deduped,
    restricted to `known` (see module docstring for why), capped at
    MAX_PANEL_SYNC_IDS."""
    if not answer or not known:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _ID_RE.finditer(answer):
        tok = m.group(0)
        if tok in known and tok not in seen:
            seen.add(tok)
            out.append(tok)
            if len(out) >= MAX_PANEL_SYNC_IDS:
                break
    return out


def turn_panel_ids(turn_tool_returns: list[tuple[str, Any]]) -> list[str]:
    """What this turn's artifacts would already put on the panel, mirroring
    the frontend's pick: the LAST search-shaped result wins; detail calls
    after it win instead (rendered as cards, one per call)."""
    last_search: list[str] | None = None
    last_search_idx = -1
    detail_ids: list[tuple[int, str]] = []
    for idx, (tool, content) in enumerate(turn_tool_returns):
        if tool in _SEARCH_TOOLS:
            last_search = _ids_from_content(content)
            last_search_idx = idx
        elif tool in _DETAIL_TOOLS:
            ids = _ids_from_content(content)
            if ids:
                detail_ids.append((idx, ids[0]))
    later_details = [i for idx, i in detail_ids if idx > last_search_idx]
    if later_details:
        return later_details
    return last_search or []


def panel_sync_ids(
    answer: str,
    turn_tool_returns: list[tuple[str, Any]],
    all_tool_returns: list[tuple[str, Any]],
    panel_ids: list[str] | None = None,
) -> list[str]:
    """The ids to synthesize a panel update for, or [] when the panel is
    already right.

    Sync only when the answer's cited set is a genuine re-presentation:
    - nothing cited → no sync (aggregate/off-graph answers leave the panel
      alone);
    - cited exactly matches what the turn already put up (same ids, same
      order) → redundant, skip;
    - cited is a strict subset of what THIS turn's own search already put on
      the panel → skip. The user asked for that search, so keep its full
      match set instead of collapsing a broad browse ("show me properties in
      X") down to the handful of ids the answer happened to name. That
      collapse is what desynced the panel — which then rendered only the
      cited rows, carrying no total_count — from the answer's total_count
      (the "chat says 14, panel shows 6" bug). Subsumes the old
      single-cited-id skip.
    - otherwise (a re-ranking of the same set, or ids resurrected from a
      PRIOR turn / trimmed history — where this turn ran no search) → sync.
    """
    known = known_auction_ids(all_tool_returns, panel_ids)
    cited = cited_ids(answer, known)
    if not cited:
        return []
    current = turn_panel_ids(turn_tool_returns)
    if cited == current:
        return []
    # A fresh same-turn search already populated the panel with its full match
    # set (via _ui_results). If the answer only re-cites a subset of those same
    # rows — naming a few examples or the "top few" — narrowing to that subset
    # would shrink the visible matches and drop the panel count below the
    # answer's total_count. Keep the search result. A genuine re-ranking has
    # the same id SET (not a strict subset), so it still syncs; a recap of a
    # prior turn has an empty `current`, so it still syncs.
    if current and set(cited) <= set(current) and len(cited) < len(current):
        return []
    return cited
