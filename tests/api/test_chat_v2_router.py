"""
tests/api/test_chat_v2_router.py
--------------------------------
The /chat/v2 endpoints: gating, the scope round-trip, the artifact shape, and
the SSE contract.

Two guarantees here are the sort that regress silently:

  * mounting /chat/v2 must not import LangChain — the whole lazy-import design
    exists because that stack costs ~28 MB on a 512 MB instance, and one
    module-scope import would spend it on every v1-only deploy;
  * the artifact shape must stay byte-identical to v1's, because the entire
    matches-panel path in web/app.js reads it unchanged.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def stub_turn(monkeypatch):
    """Replace the loop with a scripted result — no LLM, no Neo4j."""
    class _Call:
        def __init__(self, tool, args, result, ui_rows=None):
            self.tool, self.args, self.result = tool, args, result
            self.ui_rows = ui_rows or []
            self.ms, self.tier, self.error = 12, 1, None

    class _Result:
        answer = "837057 is the cheapest at Rs 35,00,000."
        recommendation = None
        tier = 1
        model_calls = 2
        input_tokens = 3052
        output_tokens = 180
        cached_tokens = 2400
        seconds = 11.2
        gate = None
        filters = {"city": "Chennai", "max_price": 4000000}
        last_total_count = 20
        last_ids = ["837057", "831476"]
        executed = [_Call("search_auctions", {"city": "Chennai"},
                          {"total_count": 20,
                           "results": [{"auction_id": "837057"}]},
                          ui_rows=[{"auction_id": str(i)} for i in range(30)])]

    captured: dict = {}

    async def fake_run_turn(question, **kwargs):
        captured["question"] = question
        captured.update(kwargs)
        return _Result()

    async def no_panel(result, panel_before=None):
        return None

    monkeypatch.setattr("api.chat.v2.loop.run_turn", fake_run_turn)
    monkeypatch.setattr("api.chat.v2.artifacts._panel_artifact", no_panel)
    return captured


# ── the lazy-import guarantee ───────────────────────────────────────────────

def test_mounting_v2_does_not_import_langchain():
    """If mounting the v2 router pulled the LangChain stack in at module
    scope, every v1-only deploy would pay ~28 MB of RSS for a code path it
    never runs — and deepagents another ~107 MB on top.

    This has to run in a clean subprocess: sibling test modules import
    LangChain directly, so checking this process's sys.modules would pass
    whatever the router does.
    """
    probe = textwrap.dedent("""
        import json, os, sys
        for k, v in [("NEO4J_URI", "bolt://f:7687"), ("NEO4J_USERNAME", "f"),
                     ("NEO4J_PASSWORD", "f"), ("OPENROUTER_API_KEY", "sk-t"),
                     ("OPENROUTER_MODEL", "f/m"), ("OPENAI_API_KEY", "f"),
                     ("SUPABASE_URL", "https://f.supabase.co"),
                     ("SUPABASE_ANON_KEY", "f"), ("APP_ENV", "test")]:
            os.environ.setdefault(k, v)
        import api.main
        heavy = {"langchain", "langgraph", "langchain_openai", "langchain_core",
                 "deepagents"}
        loaded = sorted({m.split(".")[0] for m in sys.modules} & heavy)
        paths = sorted({r.path for r in api.main.app.routes
                        if getattr(r, "path", "").startswith("/chat/v2")})
        print(json.dumps({"loaded": loaded, "paths": paths}))
    """)
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    report = json.loads(proc.stdout.strip().splitlines()[-1])

    assert report["loaded"] == [], (
        f"{report['loaded']} imported at startup — the v2 stack must stay lazy")
    assert report["paths"] == ["/chat/v2", "/chat/v2/stream"]


def test_both_endpoints_are_registered():
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert {"/chat/v2", "/chat/v2/stream"} <= paths


def test_v1_endpoints_are_untouched():
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert {"/chat", "/chat/stream"} <= paths


# ── the blocking endpoint ───────────────────────────────────────────────────

def test_answer_and_usage(client, stub_turn):
    body = client.post("/chat/v2", json={"message": "cheapest in Chennai"}).json()

    assert body["answer"].startswith("837057")
    assert body["usage"]["llm_calls"] == 2
    assert body["usage"]["tier"] == 1
    assert body["usage"]["cached_tokens"] == 2400


def test_scope_round_trips_instead_of_a_transcript(client, stub_turn):
    """The client stores and echoes one small dict where v1 round-trips the
    whole message history — the reason turn five costs the same as turn one."""
    body = client.post("/chat/v2", json={
        "message": "under 40 lakhs",
        "scope": {"filters": {"city": "Chennai"}, "last_ids": ["837057"],
                  "last_total_count": 66, "turn": 2},
    }).json()

    assert stub_turn["scope"] == {"city": "Chennai"}
    assert stub_turn["last_ids"] == ["837057"]
    assert stub_turn["last_total_count"] == 66
    assert body["scope"]["filters"] == {"city": "Chennai", "max_price": 4000000}
    assert body["scope"]["turn"] == 3
    assert "message_history" not in body


def test_client_supplied_scope_is_sanitized(client, stub_turn):
    """The scope is merged into search_auctions kwargs by code, so an
    off-contract key from a tampered client is filter injection."""
    client.post("/chat/v2", json={
        "message": "hi",
        "scope": {"filters": {"city": "Chennai", "limit": 999,
                              "__proto__": "x"}},
    })
    assert stub_turn["scope"] == {"city": "Chennai"}


def test_artifacts_keep_the_v1_shape(client, stub_turn):
    """extractResultsFromArtifacts and the whole panel path in web/app.js read
    this shape unchanged."""
    body = client.post("/chat/v2", json={"message": "q"}).json()
    artifact = body["artifacts"][0]

    assert set(artifact) == {"tool", "args", "result", "ui_rows"}
    assert artifact["tool"] == "search_auctions"
    assert len(artifact["ui_rows"]) == 30


def test_plan_reports_what_actually_ran(client, stub_turn):
    body = client.post("/chat/v2", json={"message": "q"}).json()
    assert body["plan"] == [{"tool": "search_auctions",
                             "args": {"city": "Chennai"},
                             "ms": 12, "tier": 1, "error": None}]


def test_deep_research_stays_on_v1(client, stub_turn):
    """Rather than half-supporting a mode the tiered loop is not shaped for,
    v2 rejects it so the client keeps that conversation on /chat."""
    resp = client.post("/chat/v2", json={"message": "q", "mode": "deep-research"})
    assert resp.status_code == 400
    assert "/chat" in resp.json()["detail"]


def test_browse_filters_seed_the_scope(client, stub_turn):
    """Same as v1: the browse panel's 'chat about these' button seeds the
    scope, and narrowing from earlier turns layers on top."""
    client.post("/chat/v2", json={
        "message": "q",
        "active_filters": {"city": "Salem"},
        "scope": {"filters": {"max_price": 3000000}},
    })
    assert stub_turn["scope"] == {"city": "Salem", "max_price": 3000000}


# ── the stream ──────────────────────────────────────────────────────────────

def _events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        name = payload = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
        if name:
            out.append((name, payload))
    return out


def test_stream_emits_the_v1_event_vocabulary(client, monkeypatch, stub_turn):
    """status / delta / final — the same names v1 uses, so the browser needs
    no new event handling."""
    class _StubResult:
        answer = "837057 is cheapest."
        recommendation = None
        tier = 1
        model_calls = 2
        input_tokens = 100
        output_tokens = 10
        cached_tokens = 0
        seconds = 1.0
        gate = None
        filters = {}
        last_total_count = None
        last_ids = []
        executed = []

    async def fake_run_turn(question, *, on_event=None, **kwargs):
        if on_event:
            on_event("status", {"label": "Planning…"})
            on_event("status", {"label": "search auctions · 20 match"})
        return _StubResult()

    monkeypatch.setattr("api.chat.v2.loop.run_turn", fake_run_turn)

    with client.stream("POST", "/chat/v2/stream", json={"message": "q"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _events("".join(resp.iter_text()))

    names = [n for n, _ in events]
    assert names.count("status") == 2
    assert "delta" in names
    assert names[-1] == "final"
    labels = [p["label"] for n, p in events if n == "status"]
    assert labels == ["Planning…", "search auctions · 20 match"]


def test_stream_reports_failure_in_band(client, monkeypatch):
    """HTTP is already 200 by the time the loop fails, so errors ride the
    stream — same contract as v1."""
    async def boom(question, **kwargs):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr("api.chat.v2.loop.run_turn", boom)

    with client.stream("POST", "/chat/v2/stream", json={"message": "q"}) as resp:
        events = _events("".join(resp.iter_text()))

    assert events[-1][0] == "error"
    assert "please retry" in events[-1][1]["detail"]
