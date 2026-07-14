"""Tests for api/chat/panel.py — the programmatic matches-panel sync that
replaced the select_properties TOOL. The model cites auction_ids in its
answer (role rule 1); the system extracts them against the conversation's
known-id set and decides whether to synthesize a panel update. Pure
functions, no I/O — the router owns the one Neo4j fetch.

Loaded by file path (not `from api.chat.panel import ...`) so the test
stays dependency-free: importing the api.chat package pulls in the router
and its pydantic_ai/fastapi deps, which this module doesn't need."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PANEL_PY = Path(__file__).resolve().parents[2] / "api" / "chat" / "panel.py"
_spec = importlib.util.spec_from_file_location("panel_under_test", _PANEL_PY)
_panel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_panel)

cited_ids = _panel.cited_ids
known_auction_ids = _panel.known_auction_ids
panel_sync_ids = _panel.panel_sync_ids
turn_panel_ids = _panel.turn_panel_ids


def _search_return(ids: list[str]) -> tuple[str, dict]:
    return ("search_auctions", {
        "total_count": len(ids),
        "results": [{"auction_id": i} for i in ids],
    })


def _detail_return(aid: str) -> tuple[str, dict]:
    return ("get_auction_detail", {"auction_id": aid, "title": "x"})


def _semantic_return(ids: list[str]) -> tuple[str, dict]:
    return ("semantic_search", {
        "returned": len(ids), "limit": len(ids),
        "results": [{"auction_id": i} for i in ids],
    })


# ── known-id gathering ─────────────────────────────────────────────────────

def test_known_ids_cover_rows_stubs_details_and_panel():
    returns = [
        _search_return(["111111", "222222"]),
        _detail_return("333333"),
        # trimmed-history breadcrumb stub
        ("search_auctions", {"_trimmed": True, "auction_ids": ["444444"]}),
    ]
    known = known_auction_ids(returns, panel_ids=["555555"])
    assert known == {"111111", "222222", "333333", "444444", "555555"}


# ── citation extraction ────────────────────────────────────────────────────

def test_cited_ids_ordered_deduped_and_restricted_to_known():
    known = {"750879", "701641"}
    answer = (
        "The best pick is **750879** (PIN 600001, reserve 2750879). "
        "Runner-up: 701641. To recap, 750879 wins."
    )
    # 600001 is a PIN and 2750879 has no word boundary as a known id —
    # only known ids are extracted, first-mention order, deduped.
    assert cited_ids(answer, known) == ["750879", "701641"]


def test_cited_ids_empty_without_known_set():
    assert cited_ids("ids 123456 and 654321", set()) == []


# ── what the turn already shows ────────────────────────────────────────────

def test_turn_panel_last_search_wins():
    turn = [_search_return(["1111", "2222"]), _search_return(["3333"])]
    assert turn_panel_ids(turn) == ["3333"]


def test_turn_panel_details_after_search_win():
    turn = [_search_return(["1111", "2222"]), _detail_return("2222")]
    assert turn_panel_ids(turn) == ["2222"]


# ── the sync decision ──────────────────────────────────────────────────────

def test_subset_recap_syncs():
    """'Top two of those' with no new search — the classic case."""
    history = [_search_return(["1111", "2222", "3333", "4444"])]
    ids = panel_sync_ids("Best two: 3333 then 1111.", [], history)
    assert ids == ["3333", "1111"]


def test_no_citations_no_sync():
    history = [_search_return(["1111", "2222"])]
    assert panel_sync_ids("There are 42 auctions in total.", [], history) == []


def test_exact_match_with_turn_result_skips():
    """The turn's own search already put exactly these ids up — redundant."""
    turn = [_search_return(["1111", "2222"])]
    assert panel_sync_ids("See 1111 and 2222.", turn, turn) == []


def test_single_cited_id_already_shown_skips():
    """'Which is cheapest?' → one id cited — don't yank the browsing list
    down to one card."""
    turn = [_search_return(["1111", "2222", "3333"])]
    assert panel_sync_ids("Cheapest is 2222.", turn, turn) == []


def test_subset_of_same_turn_search_stays_whole():
    """The reported 'chat says 14, panel shows 6' bug: a fresh broad search
    this turn found many rows; the answer names only a few as example
    listings (+ an outlier). Narrowing the panel to that handful would
    collapse the browse and drop the panel count below the answer's
    total_count — so keep the full search result (no sync)."""
    turn = [_search_return([f"{100000 + i}" for i in range(10)])]
    answer = (
        "Found **14 properties** in Ambattur. Sample listings: "
        "100000, 100001, 100002, 100003, 100004. "
        "One outlier: 100005."
    )
    assert panel_sync_ids(answer, turn, turn) == []


def test_reranking_same_ids_syncs():
    """Same ids, different order = a re-ranking the panel should mirror."""
    turn = [_search_return(["1111", "2222"])]
    ids = panel_sync_ids("Ranked: 2222 first, then 1111.", turn, turn)
    assert ids == ["2222", "1111"]


def test_ids_from_trimmed_history_sync_without_turn_tools():
    """Answering from a prior turn's (now trimmed) results — no tool call
    this turn, but the panel should still follow the citations."""
    history = [("search_auctions", {"_trimmed": True, "auction_ids": ["7777", "8888"]})]
    ids = panel_sync_ids("As before, 8888 remains the best.", [], history)
    assert ids == ["8888"]


def test_panel_ids_count_as_known():
    """Client-supplied panel state makes its ids citable even when no tool
    result in history carries them (restored conversations)."""
    ids = panel_sync_ids("Go with 9999.", [], [], panel_ids=["9999", "9998"])
    assert ids == ["9999"]


def test_research_search_on_anchored_property_reanchors():
    """The '1 match → 20 matches' bug. The user is viewing ONE property
    (panel_ids=['794846']) and asks an analytical follow-up about it ("does
    this land affected by any major development?"). The agent pulls the detail
    and then fires a broad semantic_search purely to gather context — it
    returns 20 rows that would hijack the single-property panel. The answer is
    about the anchored property only, so re-anchor the panel to it instead of
    letting the research search's rows take over."""
    prev = ["794846"]
    turn = [
        _detail_return("794846"),
        _semantic_return(["794846"] + [f"{800000 + i}" for i in range(19)]),
    ]
    answer = (
        "Is Mariputhur (ID 794846) in the Global City zone? No — the land "
        "itself isn't in the acquisition zone, but it's in the same area."
    )
    assert panel_sync_ids(answer, turn, turn, panel_ids=prev) == ["794846"]


def test_browse_subset_keeps_whole_even_with_prior_panel():
    """Guard the re-anchor rule from over-firing: when the answer names a few
    examples of a fresh browse and those cited ids are NOT the pre-turn panel,
    keep the full search result — the '14 vs 6' behaviour must survive even
    when the client supplies an unrelated prior panel."""
    prev = ["555555"]
    turn = [_search_return([f"{100000 + i}" for i in range(10)])]
    answer = (
        "Found **14 properties** in Ambattur. Sample: 100000, 100001, 100002."
    )
    assert panel_sync_ids(answer, turn, turn, panel_ids=prev) == []
