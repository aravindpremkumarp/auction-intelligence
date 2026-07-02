"""
tests/api/test_chat_model_selection.py
---------------------------------------
The user-selectable model + reasoning-effort toggles, and the server-side
entitlement gate that locks free/anonymous chat to the cheap Flash model.

Two layers:
  1. Pure logic in `api.model_selection` (resolvers + `extra_body` builder),
     tested directly — no agent, no network.
  2. The /chat + /chat/stream wiring and GET /chat/models, tested through the
     app so we prove the resolved model name actually reaches the agent run
     (and that a tampered client can't escalate off the free tier).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import auth_header


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


# ── Pure resolvers ────────────────────────────────────────────────────────────
def test_free_and_anon_are_locked_to_flash() -> None:
    from api.model_selection import resolve_chat_model

    # Anon (tier None) and free can never reach Pro, even asking explicitly.
    assert resolve_chat_model(None, "pro") == "flash"
    assert resolve_chat_model("free", "pro") == "flash"
    assert resolve_chat_model("free", None) == "flash"


def test_paid_honors_request_else_defaults_to_pro() -> None:
    from api.model_selection import resolve_chat_model

    assert resolve_chat_model("paid", "flash") == "flash"
    assert resolve_chat_model("paid", "pro") == "pro"
    assert resolve_chat_model("paid", None) == "pro"   # default
    assert resolve_chat_model("paid", "bogus") == "pro"  # unknown -> default


def test_resolve_reasoning_effort_validates() -> None:
    from api.model_selection import FREE_TIER_EFFORT, resolve_reasoning_effort

    # Paid: requested value is normalised/validated; None -> server default.
    assert resolve_reasoning_effort("HIGH", "paid") == "high"   # normalised
    assert resolve_reasoning_effort("off", "paid") == "off"
    assert resolve_reasoning_effort(None, "paid") is None       # -> server default
    assert resolve_reasoning_effort("turbo", "paid") is None     # unknown -> default
    # Free/anon: capped server-side — requests ABOVE the ceiling clamp down.
    assert resolve_reasoning_effort("high", "free") == FREE_TIER_EFFORT
    assert resolve_reasoning_effort("high", None) == FREE_TIER_EFFORT  # anon
    assert resolve_reasoning_effort(None, "free") == FREE_TIER_EFFORT  # no toggle
    assert resolve_reasoning_effort("turbo", "free") == FREE_TIER_EFFORT  # unknown


def test_free_tier_off_is_honored() -> None:
    """Regression: the ceiling must not OVERRIDE a cheaper request. Off is the
    cheapest setting — a free/anon user turning thinking off must get off, not
    the cap ("toggle Off but it still thinks")."""
    from api.model_selection import resolve_reasoning_effort

    assert resolve_reasoning_effort("off", "free") == "off"
    assert resolve_reasoning_effort("off", None) == "off"    # anonymous
    assert resolve_reasoning_effort("none", "free") == "none"


def test_free_tier_ceiling_honors_below_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a raised cap (e.g. medium), a below-cap request (low) is honored."""
    from api import model_selection as ms

    monkeypatch.setattr(ms, "FREE_TIER_EFFORT", "medium")
    assert ms.resolve_reasoning_effort("low", "free") == "low"
    assert ms.resolve_reasoning_effort("high", "free") == "medium"  # above -> clamp


def test_build_model_settings_reasoning_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import model_selection as ms

    # Explicit effort sets the reasoning block.
    assert ms.build_model_settings("high")["extra_body"]["reasoning"] == {"effort": "high"}
    # Regression: "off" must EXPLICITLY disable reasoning. Omitting the block
    # leaves hybrid models (DeepSeek V4) on the provider default, which can be
    # reasoning ON — the "toggle Off but it still thinks" bug.
    assert ms.build_model_settings("off")["extra_body"]["reasoning"] == {"enabled": False}
    assert ms.build_model_settings("none")["extra_body"]["reasoning"] == {"enabled": False}
    # A server default of "off" disables the same way.
    monkeypatch.setattr(ms, "OPENROUTER_CHAT_REASONING_EFFORT", "off")
    assert ms.build_model_settings(None)["extra_body"]["reasoning"] == {"enabled": False}
    # None falls back to the server default (high).
    monkeypatch.setattr(ms, "OPENROUTER_CHAT_REASONING_EFFORT", "high")
    assert ms.build_model_settings(None)["extra_body"]["reasoning"] == {"effort": "high"}
    # usage accounting is always on (so the obs log can report cost).
    assert ms.build_model_settings(None)["extra_body"]["usage"] == {"include": True}


# ── End-to-end gating (the model name that reaches agent.run) ─────────────────
@pytest.fixture
def captured_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict]:
    """A /chat client whose agent + override-builder are stubbed so we can
    assert which logical model the router resolved for the caller's tier."""
    import importlib

    chat_router = importlib.import_module("api.chat.router")
    from api.main import app

    captured: dict[str, Any] = {}

    def _fake_overrides(model_name=None, reasoning_effort=None):
        captured["model"] = model_name
        captured["reasoning_effort"] = reasoning_effort
        return {}  # no real model override -> the fake agent below runs

    class _Res:
        output = "ok"
        def new_messages(self): return []
        def all_messages(self): return []

    class _Agent:
        async def run(self, *a: Any, **kw: Any) -> Any:
            return _Res()

    monkeypatch.setattr(chat_router, "build_chat_run_overrides", _fake_overrides)
    monkeypatch.setattr(chat_router, "agent", _Agent())
    return TestClient(app), captured


def test_free_user_request_for_pro_is_downgraded(
    captured_client: tuple[TestClient, dict],
) -> None:
    client, captured = captured_client
    h = auth_header(sub="sub-free-toggle", email="free-toggle@x.com")
    resp = client.post(
        "/chat",
        json={"message": "hi", "model": "pro", "reasoning_effort": "high"},
        headers=h,
    )
    assert resp.status_code == 200
    # Server forced Flash despite the client asking for Pro, and clamped the
    # reasoning effort down despite the client asking for "high".
    from api.model_selection import FREE_TIER_EFFORT

    assert captured["model"] == "flash"
    assert captured["reasoning_effort"] == FREE_TIER_EFFORT


def test_anonymous_chat_uses_flash(captured_client: tuple[TestClient, dict]) -> None:
    client, captured = captured_client
    resp = client.post("/chat", json={"message": "hi", "model": "pro"})
    assert resp.status_code == 200
    assert captured["model"] == "flash"


def test_free_user_off_reaches_the_model(
    captured_client: tuple[TestClient, dict],
) -> None:
    """End-to-end regression for "toggle Off but it still thinks": a free
    user's reasoning_effort="off" must survive the tier gate all the way to
    the agent overrides, not be swapped for the free-tier cap."""
    client, captured = captured_client
    h = auth_header(sub="sub-free-off", email="free-off@x.com")
    resp = client.post(
        "/chat",
        json={"message": "hi", "reasoning_effort": "off"},
        headers=h,
    )
    assert resp.status_code == 200
    assert captured["reasoning_effort"] == "off"


def test_paid_user_can_select_pro(
    captured_client: tuple[TestClient, dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, captured = captured_client
    import api.neo4j_client as neo

    sub = "sub-paid-toggle"
    h = auth_header(sub=sub, email="paid-toggle@x.com")
    client.get("/auth/me", headers=h)  # materialise the :User row
    neo._users[sub]["plan_expires_at"] = _iso(
        datetime.now(timezone.utc) + timedelta(days=30)
    )

    resp = client.post("/chat", json={"message": "hi", "model": "pro"}, headers=h)
    assert resp.status_code == 200
    assert captured["model"] == "pro"


# ── GET /chat/models ──────────────────────────────────────────────────────────
def test_models_endpoint_locks_pro_for_free() -> None:
    from api.main import app

    client = TestClient(app)
    body = client.get("/chat/models").json()
    assert body["tier"] == "free"
    by_id = {m["id"]: m for m in body["models"]}
    assert by_id["flash"]["locked"] is False
    assert by_id["pro"]["locked"] is True
    assert body["defaults"]["model"] == "flash"
    # Free default effort reflects the server-side clamp, not the paid "high".
    from api.model_selection import FREE_TIER_EFFORT

    assert body["defaults"]["reasoning_effort"] == FREE_TIER_EFFORT
    assert [e["id"] for e in body["reasoning_efforts"]] == ["off", "medium", "high"]
    # Efforts above the free ceiling are locked (they'd silently clamp);
    # Off is at/below the ceiling and stays available.
    eff = {e["id"]: e for e in body["reasoning_efforts"]}
    assert eff["off"]["locked"] is False
    assert eff["medium"]["locked"] is True
    assert eff["high"]["locked"] is True


def test_models_endpoint_unlocks_pro_for_paid() -> None:
    from api.main import app
    import api.neo4j_client as neo

    sub = "sub-paid-models"
    h = auth_header(sub=sub, email="paid-models@x.com")
    client = TestClient(app)
    client.get("/auth/me", headers=h)
    neo._users[sub]["plan_expires_at"] = _iso(
        datetime.now(timezone.utc) + timedelta(days=30)
    )

    body = client.get("/chat/models", headers=h).json()
    assert body["tier"] == "paid"
    by_id = {m["id"]: m for m in body["models"]}
    assert by_id["pro"]["locked"] is False
    assert body["defaults"]["model"] == "pro"
    # Paid users get the full effort range — nothing locked.
    assert all(e["locked"] is False for e in body["reasoning_efforts"])
