"""Tests for api/agent3/router.py and artifacts.py — the /chat/agent3 surface.

No model, no network, no Neo4j: the loop is stubbed and the auth dependency
overridden. What is pinned is the contract the frontend and the other three
chat endpoints depend on.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ── the import rule the whole package is arranged around ─────────────────

def test_the_router_is_not_reachable_from_the_package_init():
    """`api/agent3/__init__.py` must not import the router.

    The tools have to stay importable with only the Neo4j driver — that is
    why this package sits outside `api/chat/` at all, and why the checkpointer
    was moved out of it. Adding a FastAPI router INSIDE the package is safe
    only as long as nothing pulls it in by importing the package itself.
    """
    tree = ast.parse((_REPO_ROOT / "api" / "agent3" / "__init__.py").read_text())
    imported = [n for n in ast.walk(tree)
                if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not imported, "api/agent3/__init__.py must stay import-free"


def test_the_tools_still_import_without_fastapi():
    """The regression this guards actually happened once, in the other
    direction: memory was moved to opt-out and `api/chat/deep/checkpointer.py`
    turned out to drag FastAPI in through `api/chat/__init__.py`. A router
    living beside the tools is the obvious way to reintroduce it."""
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent("""
        import sys
        import api.agent3.find_properties   # noqa: F401
        import api.agent3.get_property      # noqa: F401
        import api.agent3.loop              # noqa: F401
        import api.agent3.gates             # noqa: F401
        print("fastapi" if "fastapi" in sys.modules else "")
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=180, cwd=str(_REPO_ROOT))
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "", "importing an agent3 tool pulled in FastAPI"


# ── thread ids ───────────────────────────────────────────────────────────

def test_a_malformed_thread_id_mints_a_fresh_one_instead_of_erroring():
    """It reaches Neo4j as a MERGE property. Losing one turn's memory is a
    far better failure than refusing to answer."""
    from api.agent3.router import _thread_id

    for bad in ["../etc/passwd", "a" * 100, "drop; MATCH (n)", "", None,
                "has spaces", "semi;colon"]:
        out = _thread_id(bad)
        assert out.startswith("agent3-")
        assert all(c.isalnum() or c in "-_" for c in out)


def test_a_good_thread_id_is_kept_so_memory_survives():
    from api.agent3.router import _thread_id

    assert _thread_id("conv_abc-123") == "conv_abc-123"


def test_thread_ids_are_unique_per_mint():
    from api.agent3.router import _thread_id

    assert _thread_id(None) != _thread_id(None)


# ── artifacts: the panel contract ────────────────────────────────────────

class _Result:
    def __init__(self, answer="", panel_rows=None, **kw):
        self.answer = answer
        self.panel_rows = panel_rows or []
        self.auction_ids = kw.get("auction_ids", [])
        self.skills_loaded = kw.get("skills_loaded", [])
        self.model_calls = kw.get("model_calls", 1)
        self.tool_calls = kw.get("tool_calls", 0)
        self.seconds = 1.0
        self.usage = kw.get("usage", {})
        self.gate_repairs = kw.get("gate_repairs", 0)
        self.gate_repaired = kw.get("gate_repaired", [])
        self.gate_findings = kw.get("gate_findings", {})


def test_the_sink_rows_become_the_panel_without_any_guessing():
    """v1 and v2 have to INFER the panel by parsing the answer for cited ids
    (`panel.py::panel_sync_ids`). agent3 does not: the sink recorded the full
    match set the model never saw, so the panel is exact."""
    from api.agent3.artifacts import build_artifacts

    rows = [{"auction_id": "748779"}, {"auction_id": "744314"}]
    arts = asyncio.run(build_artifacts(_Result(answer="Two matches.", panel_rows=rows)))
    assert len(arts) == 1
    assert arts[0]["tool"] == "find_properties"
    assert arts[0]["ui_rows"] == rows
    assert arts[0]["result"]["total_count"] == 2


def test_the_panel_gets_every_match_not_the_models_sample():
    """The asymmetry the sink exists for: the model saw 10 rows, the panel
    must still show all 200."""
    from api.agent3.artifacts import build_artifacts

    rows = [{"auction_id": f"{700000 + i}"} for i in range(200)]
    arts = asyncio.run(build_artifacts(_Result(answer="200 matches.", panel_rows=rows)))
    assert len(arts[0]["ui_rows"]) == 200


def test_a_by_id_turn_falls_back_to_fetching_the_cited_listings(monkeypatch):
    """get_property / benchmark_price / reauction_history reach the graph by
    id and put nothing in the sink, so the cited ids ARE the panel."""
    from api.agent3 import artifacts as A
    from api.tools import cypher_tools as cypher_T

    monkeypatch.setattr(cypher_T, "get_auctions_by_ids",
                        lambda ids: {"rows": [{"auction_id": i} for i in ids]})
    arts = asyncio.run(A.build_artifacts(
        _Result(answer="Auction 748779 is in Coimbatore.")))
    assert arts[0]["tool"] == "select_properties"
    assert arts[0]["args"]["auction_ids"] == ["748779"]


def test_an_unchanged_panel_is_not_re_sent(monkeypatch):
    """Re-sending an identical set makes the panel flicker for no change."""
    from api.agent3 import artifacts as A

    monkeypatch.setattr(A, "cited_ids", lambda a: ["748779"])
    arts = asyncio.run(A.build_artifacts(_Result(answer="748779 again."),
                                   panel_before=["748779"]))
    assert arts == []


def test_an_answer_citing_nothing_leaves_the_panel_alone():
    """Aggregate answers ("35 in Coimbatore") name no listing. Clearing the
    panel for them would throw away what the user was looking at."""
    from api.agent3.artifacts import build_artifacts

    assert asyncio.run(build_artifacts(_Result(answer="There are 35 in total."))) == []


def test_a_broken_panel_never_fails_a_good_answer(monkeypatch):
    """The panel is cosmetic. A failure here taking down a correct answer is
    the worst available trade."""
    from api.agent3 import artifacts as A

    def boom(*a, **k):
        raise RuntimeError("neo4j is down")

    monkeypatch.setattr(A, "cited_ids", boom)
    assert asyncio.run(A.build_artifacts(_Result(answer="Auction 748779."))) == []


def test_a_price_is_not_mistaken_for_a_listing_id():
    """₹6,50,000 normalises to a six-digit number. Fetching it as an id would
    put an unrelated listing in the panel."""
    from api.agent3.artifacts import cited_ids

    assert cited_ids("The reserve is 4,641,000 rupees.") == []
    assert cited_ids("Auction 748779 costs ₹46,41,000.") == ["748779"]


# ── the response shape ───────────────────────────────────────────────────

def test_the_response_omits_scope_and_plan_rather_than_faking_them():
    """/chat/deep fills `scope` with turn=0 and carries filters "for the
    matches panel only". Here the transcript IS the memory, so a scope object
    beside it is a second source of truth — the exact shape of bug that put
    memory on the server. Better absent than fabricated."""
    from api.agent3.router import ChatAgent3Response

    fields = set(ChatAgent3Response.model_fields)
    assert "scope" not in fields
    assert "plan" not in fields
    assert {"answer", "thread_id", "artifacts", "usage"} <= fields


def test_the_gate_result_reaches_the_caller():
    """`repairs` above zero means a draft was rejected and rewritten. The
    offending draft is deleted, so without `repaired` there is no trace of
    what was caught."""
    from api.agent3.router import GateOut

    g = GateOut(repairs=1, repaired=["cites auction_id(s) 827001"],
                advisory=["unverified amount '₹60L'"])
    assert g.repairs == 1 and "827001" in g.repaired[0]


# ── end to end, through the real app ─────────────────────────────────────

def _client(monkeypatch, result):
    """A TestClient with auth and the loop stubbed, everything else real.

    Deliberately exercises the actual FastAPI app rather than calling the
    handler: mounting, the response model, and the auth dependency are three
    places this can be wired wrong and unit tests would not notice.
    """
    from fastapi.testclient import TestClient

    from api.agent3 import router as R
    from api.auth import get_current_admin
    from api.main import app

    async def fake_run_turn(message, **kw):
        fake_run_turn.seen = {"message": message, **kw}
        return result

    async def no_quota(*a, **k):
        return None

    import api.agent3.loop as L
    monkeypatch.setattr(L, "run_turn", fake_run_turn)
    monkeypatch.setattr(R, "enforce_chat_quota", no_quota)
    monkeypatch.setattr(R, "_saver", lambda: object())
    app.dependency_overrides[get_current_admin] = lambda: None
    client = TestClient(app)
    client.fake = fake_run_turn
    return client


def _teardown():
    from api.main import app
    app.dependency_overrides.clear()


def test_a_turn_returns_the_answer_and_the_panel(monkeypatch):
    result = _Result(answer="Auction 748779 is in Coimbatore.",
                     panel_rows=[{"auction_id": "748779"}],
                     skills_loaded=["diligence"], tool_calls=1,
                     usage={"input_tokens": 100, "output_tokens": 20,
                            "cached_input_tokens": 60})
    client = _client(monkeypatch, result)
    try:
        r = client.post("/chat/agent3", json={"message": "tell me about 748779",
                                              "thread_id": "conv_1"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["answer"] == "Auction 748779 is in Coimbatore."
        assert body["thread_id"] == "conv_1"
        assert body["skills"] == ["diligence"]
        assert body["artifacts"][0]["ui_rows"] == [{"auction_id": "748779"}]
        assert body["usage"]["cached_tokens"] == 60
    finally:
        _teardown()


def test_the_thread_id_reaches_the_loop_so_memory_works(monkeypatch):
    """The whole memory design hangs off this one argument."""
    client = _client(monkeypatch, _Result(answer="ok"))
    try:
        client.post("/chat/agent3", json={"message": "hi", "thread_id": "conv_42"})
        assert client.fake.seen["thread_id"] == "conv_42"
    finally:
        _teardown()


def test_a_first_turn_without_a_thread_id_gets_one_minted(monkeypatch):
    """The client keeps it for the rest of the conversation."""
    client = _client(monkeypatch, _Result(answer="ok"))
    try:
        body = client.post("/chat/agent3", json={"message": "hi"}).json()
        assert body["thread_id"].startswith("agent3-")
    finally:
        _teardown()


def test_an_empty_message_is_rejected(monkeypatch):
    client = _client(monkeypatch, _Result(answer="ok"))
    try:
        r = client.post("/chat/agent3", json={"message": "   "})
        assert r.status_code == 400
    finally:
        _teardown()


def test_an_oversized_message_is_rejected(monkeypatch):
    """An unbounded prompt is prepended to a cached prefix and re-sent on
    every later turn of the thread — a permanent tax, not a one-off."""
    from api.agent3.router import MAX_MESSAGE_CHARS

    client = _client(monkeypatch, _Result(answer="ok"))
    try:
        r = client.post("/chat/agent3",
                        json={"message": "x" * (MAX_MESSAGE_CHARS + 1)})
        assert r.status_code == 400
    finally:
        _teardown()


def test_the_gate_diagnostics_reach_the_response(monkeypatch):
    result = _Result(answer="ok", gate_repairs=1,
                     gate_repaired=["cites auction_id(s) 827001"],
                     gate_findings={"advisory": ["unverified amount"]})
    client = _client(monkeypatch, result)
    try:
        body = client.post("/chat/agent3", json={"message": "hi"}).json()
        assert body["gate"]["repairs"] == 1
        assert "827001" in body["gate"]["repaired"][0]
        assert body["gate"]["advisory"] == ["unverified amount"]
    finally:
        _teardown()


def test_the_other_three_chat_surfaces_are_untouched():
    """The clean slate was never licence to disturb what already works."""
    from api.main import app

    paths = {r.path for r in app.routes}
    for path in ["/chat", "/chat/stream", "/chat/v2", "/chat/v2/stream",
                 "/chat/deep", "/chat/deep/stream"]:
        assert path in paths, f"{path} disappeared"
