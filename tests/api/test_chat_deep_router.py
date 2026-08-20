"""
tests/api/test_chat_deep_router.py
----------------------------------
`/chat/deep` and `/chat/deep/stream` — the wire contract, the admin gate, the
thread-key trust boundary, and the import discipline that keeps the deploy
inside 512 MB.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

from api.chat.deep.router import _thread_id
from api.main import app


@pytest.fixture
def admin_headers():
    """/chat/deep is admin-gated for the same reason /chat/v2 is: every
    request spends real money on the prepaid OpenRouter key."""
    from api import neo4j_client
    from tests.api.conftest import auth_header

    c = TestClient(app)
    h = auth_header(sub="lab-admin", email="admin@example.com", name="Admin")
    c.get("/auth/me", headers=h)
    neo4j_client._users["lab-admin"]["role"] = "admin"  # type: ignore[attr-defined]
    return h


@pytest.fixture
def client(admin_headers):
    c = TestClient(app)
    c.headers.update(admin_headers)
    return c


# ── the thread key is a trust boundary ──────────────────────────────────────

def test_a_normal_conversation_id_passes_through():
    assert _thread_id("9f2b1c44-77aa-4e1e-b0d1-1a2b3c4d5e6f") == \
        "9f2b1c44-77aa-4e1e-b0d1-1a2b3c4d5e6f"
    assert _thread_id("conv_42-b") == "conv_42-b"


@pytest.mark.parametrize("hostile", [
    "'} DETACH DELETE n //",       # Cypher injection shaped
    "../../etc/passwd",
    "a" * 65,                       # over the length cap
    "  ",
    None,
    "",
    "has space",
])
def test_a_malformed_thread_key_mints_a_fresh_thread(hostile):
    """It is client-supplied and lands in a MERGE. A bad key must degrade to a
    new thread — losing one turn's memory — rather than erroring, and must
    never reach Cypher as-is."""
    out = _thread_id(hostile)
    assert out.startswith("deep-")
    assert out != hostile


def test_minted_keys_are_unique():
    assert _thread_id(None) != _thread_id(None)


# ── the endpoint ────────────────────────────────────────────────────────────

class _Call:
    def __init__(self, tool, args, result, ui_rows=None):
        self.tool, self.args, self.result = tool, args, result
        self.ui_rows = ui_rows or []
        self.ms, self.tier, self.error = 12, 0, None

    def as_dict(self):
        return {"tool": self.tool, "args": self.args, "result": self.result}


class _Result:
    answer = "837057 is the cheapest at Rs 16,10,000."
    recommendation = None
    tier = 0
    steps = 6
    model_calls = 4
    input_tokens = 21000
    output_tokens = 400
    cached_tokens = 15000
    seconds = 24.1
    gate = None
    filters = {"city": "Chennai"}
    last_total_count = 11
    last_ids = ["837057"]
    last_question = "cheapest flats in Chennai"
    last_entities = {"area": ["Ambattur"]}
    executed = [_Call("search_auctions", {"city": "Chennai"},
                      {"total_count": 11, "results": [{"auction_id": "837057"}]})]


@pytest.fixture
def stub_turn(monkeypatch):
    captured: dict = {}

    async def fake_run_turn(question, **kwargs):
        captured["question"] = question
        captured.update(kwargs)
        return _Result()

    async def no_panel(result, panel_before=None):
        return None

    monkeypatch.setattr("api.chat.deep.loop.run_turn", fake_run_turn)
    monkeypatch.setattr("api.chat.v2.artifacts._panel_artifact", no_panel)
    return captured


def test_answer_thread_and_usage_come_back(client, stub_turn):
    body = client.post("/chat/deep", json={"message": "cheapest flats",
                                           "thread_id": "conv-7"}).json()

    assert body["answer"].startswith("837057")
    assert body["thread_id"] == "conv-7"
    assert body["steps"] == 6
    assert body["usage"]["llm_calls"] == 4
    assert body["usage"]["steps"] == 6


def test_the_thread_id_is_what_reaches_the_loop(client, stub_turn):
    client.post("/chat/deep", json={"message": "q", "thread_id": "conv-7"})
    assert stub_turn["thread_id"] == "conv-7"
    # And no scope: this endpoint round-trips neither a transcript nor a
    # summary. Server-owned state is the whole point.
    assert "scope" not in stub_turn


def test_a_first_turn_without_a_thread_id_gets_one_minted(client, stub_turn):
    body = client.post("/chat/deep", json={"message": "q"}).json()
    assert body["thread_id"].startswith("deep-")
    assert stub_turn["thread_id"] == body["thread_id"]


def test_scope_is_returned_for_the_panel_only(client, stub_turn):
    """The matches panel reads `last_ids`, so the field stays on the wire even
    though this loop's memory is the checkpointed transcript."""
    body = client.post("/chat/deep", json={"message": "q"}).json()
    assert body["scope"]["last_ids"] == ["837057"]
    assert body["scope"]["last_total_count"] == 11


def test_artifacts_keep_the_v1_shape(client, stub_turn):
    """`extractResultsFromArtifacts` and `setPanelSource` in web/app.js must
    work against all three loops unchanged."""
    body = client.post("/chat/deep", json={"message": "q"}).json()
    assert body["artifacts"][0]["tool"] == "search_auctions"
    assert set(body["artifacts"][0]) == {"tool", "args", "result", "ui_rows"}


def test_deep_research_mode_is_rejected(client, stub_turn):
    r = client.post("/chat/deep", json={"message": "q", "mode": "deep-research"})
    assert r.status_code == 400
    assert "deep-research" in r.json()["detail"]


# ── import discipline ───────────────────────────────────────────────────────

def test_importing_the_app_does_not_load_deepagents():
    """`deepagents` costs ~107 MB of RSS on top of LangChain's ~28 MB, against
    a 512 MB starter instance. A module-scope import here is a deploy-time
    OOM, not a test failure — so this runs in a clean subprocess where an
    earlier test's import cannot mask it."""
    code = textwrap.dedent("""
        import sys
        import api.main  # noqa: F401
        heavy = {"deepagents", "langchain", "langgraph", "langchain_openai"}
        loaded = {m.split(".")[0] for m in sys.modules} & heavy
        print(",".join(sorted(loaded)))
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "", (
        f"api.main pulled in {out.stdout.strip()} at import time"
    )


def test_a_non_admin_is_refused(stub_turn):
    """Gating the /lab page alone would be cosmetic — anyone who knew the URL
    could POST here and spend the key."""
    from tests.api.conftest import auth_header

    c = TestClient(app)
    r = c.post("/chat/deep", json={"message": "q"},
               headers=auth_header(sub="not-admin", email="user@example.com",
                                   name="User"))
    assert r.status_code == 403


def test_forgetting_a_thread_drops_its_checkpoints(client, monkeypatch):
    """The server-side twin of starting a new chat. `apiChatScope` never being
    cleared is exactly the bug this endpoint exists to make impossible."""
    dropped: list[str] = []

    class _Saver:
        async def adelete_thread(self, thread_id):
            dropped.append(thread_id)

    monkeypatch.setattr("api.chat.deep.router._saver", lambda: _Saver())

    body = client.delete("/chat/deep/conv-7").json()

    assert body == {"thread_id": "conv-7", "forgotten": True}
    assert dropped == ["conv-7"]
