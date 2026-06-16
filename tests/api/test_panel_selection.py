"""
tests/api/test_panel_selection.py
---------------------------------
The matches panel is browser-only state the agent can't see, so a bare
"compare these" / "the matches" / "all of them" had nothing to resolve to —
the agent answered as if the conversation were empty. The fix forwards the
panel's current auction_ids to the agent as `panel_auction_ids` (ids only, so
it stays cheap) and injects them as an instruction.

Two surfaces under test:
  1. `_clean_panel_ids` — sanitizes/bounds the client-supplied list.
  2. `/chat` + `/chat/stream` — the cleaned ids actually reach `agent.run`'s
     `deps`, which is what the bug was missing.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


# ── _clean_panel_ids ───────────────────────────────────────────────────────

def test_clean_panel_ids_dedupes_and_preserves_order() -> None:
    from api.chat.router import _clean_panel_ids
    # Display order matters (the model is told to respect it); first wins.
    assert _clean_panel_ids(["A2", "A1", "A2", "A3", "A1"]) == ["A2", "A1", "A3"]


def test_clean_panel_ids_drops_empty_and_none_and_trims() -> None:
    from api.chat.router import _clean_panel_ids
    assert _clean_panel_ids(["A1", "", "  ", None, "  A2  "]) == ["A1", "A2"]


def test_clean_panel_ids_coerces_non_strings() -> None:
    from api.chat.router import _clean_panel_ids
    # A client could send numeric ids; they must become strings, not crash.
    assert _clean_panel_ids([708365, 701641]) == ["708365", "701641"]


def test_clean_panel_ids_caps_length() -> None:
    from api.chat.router import _clean_panel_ids, _MAX_PANEL_IDS
    cleaned = _clean_panel_ids([f"A{i}" for i in range(_MAX_PANEL_IDS + 25)])
    assert cleaned is not None
    assert len(cleaned) == _MAX_PANEL_IDS
    # Keeps the head of the list (the top of the ranked/sorted panel).
    assert cleaned[0] == "A0"


def test_clean_panel_ids_empty_and_bad_input_is_none() -> None:
    from api.chat.router import _clean_panel_ids
    assert _clean_panel_ids(None) is None
    assert _clean_panel_ids([]) is None
    assert _clean_panel_ids(["", "  ", None]) is None
    assert _clean_panel_ids("A1") is None  # not a list — ignore, don't iterate chars


# ── end-to-end: ids reach agent.run deps ───────────────────────────────────

class _Res:
    output = "ok"

    def new_messages(self):
        return []

    def all_messages(self):
        return []


def _capturing_agent(captured: dict[str, Any]):
    class _Agent:
        async def run(self, *a: Any, **kw: Any) -> Any:
            captured.update(kw)
            return _Res()

    return _Agent()


def test_chat_forwards_panel_ids_to_agent_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: the panel's ids land on the deps the agent runs with,
    cleaned (deduped, trimmed, empties dropped) and in order."""
    import importlib
    chat_router = importlib.import_module("api.chat.router")
    from api.main import app

    captured: dict[str, Any] = {}
    monkeypatch.setattr(chat_router, "agent", _capturing_agent(captured))

    client = TestClient(app)
    resp = client.post("/chat", json={
        "message": "compare these",
        "panel_auction_ids": ["A2", "A1", "A2", "", "  ", "A3"],
    })
    assert resp.status_code == 200
    deps = captured["deps"]
    assert deps.panel_auction_ids == ["A2", "A1", "A3"]


def test_chat_panel_ids_absent_is_none_on_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """No panel selection (empty panel) must not put an empty list on the
    deps — the instruction is gated on a truthy value so the prompt prefix
    stays byte-stable when there's nothing to inject."""
    import importlib
    chat_router = importlib.import_module("api.chat.router")
    from api.main import app

    captured: dict[str, Any] = {}
    monkeypatch.setattr(chat_router, "agent", _capturing_agent(captured))

    client = TestClient(app)
    resp = client.post("/chat", json={"message": "how many banks are there"})
    assert resp.status_code == 200
    assert captured["deps"].panel_auction_ids is None
