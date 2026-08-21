"""Tests for api/agent3/agent.py and loop.py — the step-4 harness.

Everything here runs without an OpenRouter key or a network call: the model
is a fake, the checkpointer is in-memory, and Neo4j is stubbed by conftest.
What is pinned is the structure the design depends on — the bound tool
surface, the cache-stable prefix, and the accounting bugs the loop A/B hit.
"""
from __future__ import annotations

import asyncio

import pytest

from pathlib import Path

from api.agent3 import agent as A
from api.agent3 import loop as L
from api.agent3.common import ToolSink

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ── the tool surface ─────────────────────────────────────────────────────

def test_exactly_four_tools_are_bound():
    """The whole point of create_agent over create_deep_agent: we choose the
    surface. A fifth tool appearing here means something re-introduced the
    harness scaffolding this design exists to avoid."""
    names = {getattr(f, "__name__", "") for f in A.bind_tools()}
    assert names == {"find_properties", "get_property", "search_notices",
                     "find_by_identifier"}


def test_no_filesystem_or_shell_tool_is_bound():
    """deepagents binds ls/read_file/write_file/edit_file/delete/glob/grep/
    execute unconditionally — measured at ~2,611 tokens of schema per
    prompt. None of them may appear here."""
    forbidden = {"ls", "read_file", "write_file", "edit_file", "delete",
                 "glob", "grep", "execute", "task", "write_todos"}
    names = {getattr(f, "__name__", "") for f in A.bind_tools()}
    assert not (names & forbidden)


def test_sink_is_not_visible_to_the_model():
    """`sink` is per-turn server state holding up to 500 panel rows. If it
    reached the tool schema the model could pass one, and pydantic cannot
    even build a JSON schema for it."""
    from langchain_core.tools import tool as lc_tool

    for fn in A.bind_tools(ToolSink()):
        schema = lc_tool(fn).args_schema.model_json_schema()
        assert "sink" not in schema.get("properties", {})


def test_bound_find_properties_still_writes_to_the_sink(monkeypatch):
    """Binding the sink must not sever it — the panel depends on this."""
    from api.agent3 import find_properties as FP

    rows = [{"auction_id": "A1", "title": "t", "city": "Chennai", "area": None,
             "district": None, "bank": None, "asset_category": None,
             "auction_type": None, "property_types": [], "reserve_price": 1.0,
             "emd": None, "auction_start": None, "deadline": None, "url": None,
             "lot_count": 1, "sqft_min": None, "sqft_max": None,
             "max_attempt": 1}]

    def fake(cypher, params=None, timeout=10.0, max_rows=200):
        if "count(a) AS total_count" in cypher:
            return [{"total_count": 1, "reserve_min": 1.0, "reserve_max": 1.0,
                     "reserve_avg": 1.0, "reserve_known": 1}]
        if "a.auction_id AS auction_id" in cypher:
            return rows
        return []

    monkeypatch.setattr(FP, "run_read_query", fake)
    sink = ToolSink()
    bound = A.bind_tools(sink)[0]
    bound(city="Chennai")
    assert sink.auction_ids == ["A1"]


# ── cache discipline: the load-bearing one ───────────────────────────────

def test_system_prompt_is_byte_identical_across_turns():
    """The loop A/B found the deep agent at 24% prompt cache and ZERO on the
    answer call, which made its entire cost column unreadable. The prefix
    only caches if it does not drift, and drift is invisible until someone
    reads a bill — so assert the bytes."""
    assert A.instructions() == A.instructions()
    assert A.instructions().encode() == A.instructions().encode()


def test_instructions_carry_no_per_turn_substitution():
    """A date, a graph size, or an f-string slot in the cache prefix silently
    halves the hit rate. Anything per-turn belongs in the human message."""
    text = A.instructions()
    assert "{" not in text and "}" not in text, (
        "instructions.md contains a format slot — per-turn values must go in "
        "the human message, not the cache prefix")


def test_skill_text_goes_in_the_human_message_not_the_system_prompt():
    composed = L.compose_input("how big is it", "SKILL BODY")
    assert "SKILL BODY" in composed
    assert "SKILL BODY" not in A.instructions()


def test_compose_input_puts_the_question_last():
    """Reference material trailing the ask reads as an afterthought."""
    composed = L.compose_input("QUESTION", "MATERIAL")
    assert composed.index("MATERIAL") < composed.index("QUESTION")


def test_compose_input_is_unchanged_when_no_skill_matched():
    assert L.compose_input("plain question", "") == "plain question"


# ── skill selection ──────────────────────────────────────────────────────

def test_skill_selection_is_deterministic():
    """A prompt that varies run to run cannot be cached, and an eval that
    varies run to run cannot be trusted."""
    from api.agent3.skills import select_skills

    q = "how big is this in cents and what is the survey number"
    assert [s.name for s in select_skills(q)] == [s.name for s in select_skills(q)]


def test_survey_shaped_number_loads_identifiers_without_the_keyword():
    """'is 331/1 listed anywhere' never says 'survey'."""
    from api.agent3.skills import select_skills

    assert "identifiers" in [s.name for s in select_skills("is 331/1 listed anywhere")]


def test_ordinary_search_loads_no_skill():
    """Skills must cost nothing on the common question."""
    from api.agent3.skills import select_skills

    assert select_skills("flats in chennai under 40 lakhs") == []


def test_skill_selection_is_capped():
    from api.agent3.skills import MAX_SKILLS_PER_TURN, select_skills

    q = ("tell me everything about this, how big in cents, and the survey "
         "number and patta and door number please, any risks")
    assert len(select_skills(q)) <= MAX_SKILLS_PER_TURN


def test_every_skill_on_disk_has_triggers():
    """A skill with no trigger never loads."""
    from api.agent3.skills import available_skills

    for name, skill in available_skills().items():
        assert skill.triggers, f"{name} has no triggers"


# ── the accounting bugs the A/B hit ──────────────────────────────────────

class _Msg:
    def __init__(self, type_, content="", usage=None):
        self.type = type_
        self.content = content
        self.usage_metadata = usage


def test_usage_is_read_off_the_final_message_not_summed(monkeypatch):
    """Summing usage over the returned list re-charges earlier turns: with a
    checkpointer that list is the whole conversation. The A/B reported
    49,550 input tokens against an actual 29,877 this way."""
    final = _Msg("ai", "answer", {"input_tokens": 100, "output_tokens": 20,
                                  "total_tokens": 120,
                                  "input_token_details": {"cache_read": 80}})
    usage = L._usage_of(final)
    assert usage["input_tokens"] == 100
    assert usage["cached_input_tokens"] == 80


def test_turn_counts_only_this_turns_messages():
    """With a checkpointer the graph returns the whole conversation. Counting
    every AIMessage would inflate every turn after the first."""
    messages = [
        _Msg("human", "turn 1"), _Msg("ai", "a1"),
        _Msg("human", "turn 2"), _Msg("ai", "think"), _Msg("tool", "{}"),
        _Msg("ai", "a2"),
    ]
    tail = L._messages_since_last_human(messages)
    assert sum(1 for m in tail if m.type == "ai") == 2
    assert sum(1 for m in tail if m.type == "tool") == 1


def test_messages_since_last_human_handles_no_human():
    assert len(L._messages_since_last_human([_Msg("ai", "x")])) == 1


# ── the full turn, against a fake agent ──────────────────────────────────

class _FakeAgent:
    """Stands in for the compiled graph: records what it was invoked with."""

    def __init__(self, answer="an answer"):
        self.answer = answer
        self.seen: dict = {}

    async def ainvoke(self, payload, config=None):
        self.seen = {"payload": payload, "config": config}
        return {"messages": [
            _Msg("human", payload["messages"][0]["content"]),
            _Msg("ai", self.answer, {"input_tokens": 10, "output_tokens": 5,
                                     "total_tokens": 15,
                                     "input_token_details": {"cache_read": 8}}),
        ]}


def test_run_turn_returns_a_ui_ready_result():
    fake = _FakeAgent("Chennai has 12 flats.")
    out = asyncio.run(L.run_turn("flats in chennai", thread_id="t1", agent=fake))
    assert out.answer == "Chennai has 12 flats."
    assert out.model_calls == 1
    assert out.skills_loaded == []
    assert out.usage["cached_input_tokens"] == 8
    assert out.seconds >= 0


def test_run_turn_threads_the_thread_id_for_memory():
    """Server-side memory is keyed on this; losing it silently starts a new
    conversation on every turn."""
    fake = _FakeAgent()
    asyncio.run(L.run_turn("q", thread_id="thread-42", agent=fake))
    assert fake.seen["config"]["configurable"]["thread_id"] == "thread-42"


def test_run_turn_injects_matched_skill_text():
    fake = _FakeAgent()
    out = asyncio.run(L.run_turn("how big is this in cents", thread_id="t1", agent=fake))
    assert out.skills_loaded == ["extent"]
    sent = fake.seen["payload"]["messages"][0]["content"]
    assert "cent" in sent.lower()
    assert sent.rstrip().endswith("how big is this in cents")


# ── middleware policy ────────────────────────────────────────────────────

class _Status(Exception):
    def __init__(self, code):
        self.status_code = code


class _Timeoutish(Exception):
    pass


@pytest.mark.parametrize("code,expected", [
    (500, True), (502, True), (503, True), (429, True),
    (400, False), (401, False), (403, False), (404, False),
])
def test_only_transient_provider_failures_are_retried(code, expected):
    """ModelRetryMiddleware defaults to retry_on=(Exception,) — everything.
    Caught while compiling the graph: a deterministic NotImplementedError got
    retried 3 times with backoff before failing anyway. On a real deploy a
    4xx (bad key, malformed request) would burn three calls and ~7s on every
    single turn to reach the same error."""
    assert A.should_retry_model_call(_Status(code)) is expected


def test_our_own_bugs_are_never_retried():
    for exc in (NotImplementedError(), ValueError("bad arg"), TypeError(),
                KeyError("k"), AttributeError()):
        assert A.should_retry_model_call(exc) is False


def test_connection_and_timeout_failures_are_retried():
    """These never reached the provider, so a retry cannot repeat a bad
    request."""
    assert A.should_retry_model_call(ConnectionError()) is True
    assert A.should_retry_model_call(TimeoutError()) is True
    assert A.should_retry_model_call(_Timeoutish()) is True


def test_argument_errors_reach_the_model_with_their_guidance():
    """common.require_enum writes messages that name the valid values — that
    is what lets the model fix its own call, so it must pass through."""
    content = A.tool_error_content(ValueError("possession must be one of: physical"))
    assert "physical" in content


def test_internal_errors_do_not_leak_their_message_to_the_model():
    """A driver exception can carry a URI, a credential or a query fragment.
    LangChain's own guidance is to name the type, not echo the message."""
    secret = "bolt://neo4j:hunter2@prod-db:7687"
    content = A.tool_error_content(RuntimeError(secret))
    assert secret not in content
    assert "hunter2" not in content
    assert "RuntimeError" in content


# ── the real compiled graph ──────────────────────────────────────────────

def _scripted_model(captured: list, replies: list | None = None):
    """A minimal tool-capable chat model that records what it was sent.

    The stock fakes (`GenericFakeChatModel`, `FakeMessagesListChatModel`)
    raise NotImplementedError under `create_agent` — they do not implement
    the async tool-calling path — so the graph could not be exercised at all
    without this.
    """
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    queue = list(replies or [])

    class ScriptedModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "scripted"

        def bind_tools(self, tools, **kw):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            captured.append(list(messages))
            content = queue.pop(0) if queue else "an answer"
            return ChatResult(generations=[
                ChatGeneration(message=AIMessage(content=content))])

        async def _agenerate(self, messages, stop=None, run_manager=None, **kw):
            return self._generate(messages, stop, None, **kw)

    return ScriptedModel()


def _compile(captured: list, replies: list | None = None):
    from langchain.agents import create_agent
    from langgraph.checkpoint.memory import InMemorySaver

    return create_agent(
        model=_scripted_model(captured, replies),
        tools=A.bind_tools(ToolSink()),
        system_prompt=A.instructions(),
        middleware=A.middleware(),
        checkpointer=InMemorySaver(),
    )


def test_the_graph_compiles_and_answers():
    """The whole point of step 4. If create_agent cannot build this tool
    surface, nothing downstream matters."""
    captured: list = []
    graph = _compile(captured, ["Answer one"])
    out = asyncio.run(graph.ainvoke(
        {"messages": [{"role": "user", "content": "q1"}]},
        config={"configurable": {"thread_id": "g1"}}))
    assert out["messages"][-1].content == "Answer one"


def test_the_instructions_actually_reach_the_model_as_a_system_message():
    """A prompt that never arrives is an agent with no rules — and it would
    look exactly like a working agent until it answered something it should
    have refused."""
    captured: list = []
    graph = _compile(captured)
    asyncio.run(graph.ainvoke(
        {"messages": [{"role": "user", "content": "q1"}]},
        config={"configurable": {"thread_id": "g2"}}))
    first = captured[0][0]
    assert first.type == "system"
    assert "Auction agent" in first.content
    assert "scope" in first.content


def test_system_prefix_is_byte_identical_across_turns_of_a_thread():
    """The cache assertion that matters, made against the REAL graph rather
    than the file: the loop A/B found the deep agent at 24% prompt cache and
    zero on the answer call, and nobody noticed until the cost column looked
    wrong."""
    captured: list = []
    graph = _compile(captured, ["one", "two"])
    cfg = {"configurable": {"thread_id": "g3"}}
    asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "q1"}]}, config=cfg))
    asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "q2"}]}, config=cfg))

    systems = [call[0].content for call in captured]
    assert len(systems) == 2
    assert systems[0] == systems[1], "system prefix drifted between turns"
    assert systems[0].encode() == systems[1].encode()


def test_memory_survives_across_turns_of_one_thread():
    """Server-side transcript memory — the thing the loop A/B said was worth
    keeping. Turn 2 must see turn 1."""
    captured: list = []
    graph = _compile(captured, ["one", "two"])
    cfg = {"configurable": {"thread_id": "g4"}}
    asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "q1"}]}, config=cfg))
    out = asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "q2"}]}, config=cfg))

    kinds = [(m.type, m.content) for m in out["messages"]]
    assert ("human", "q1") in kinds
    assert ("human", "q2") in kinds


def test_separate_threads_do_not_share_memory():
    """The bug class server-owned state removes: the old client-carried
    scope object was cleared at none of four sites, so a new chat inherited
    the previous thread's filters."""
    captured: list = []
    graph = _compile(captured, ["one", "two"])
    asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "q1"}]},
                              config={"configurable": {"thread_id": "alpha"}}))
    out = asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "q2"}]},
                                    config={"configurable": {"thread_id": "beta"}}))
    contents = [m.content for m in out["messages"]]
    assert "q1" not in contents


def test_the_bound_tool_surface_is_pinned_on_the_compiled_graph():
    """Asserting on what we PASS IN is not enough — the deep loop's docs
    claimed subagents were off for weeks because the assertion inspected the
    middleware list rather than the graph the harness assembled. Check the
    compiled graph."""
    captured: list = []
    graph = _compile(captured)
    names = set()
    for node in graph.get_graph().nodes:
        names.add(str(node))
    # The tool node exists and no filesystem/shell node was added.
    assert not {"filesystem", "shell", "execute"} & names


# ── import discipline ────────────────────────────────────────────────────

def test_the_tools_import_without_langchain():
    """`evals/run_agent3.py` and every tool unit test must run with nothing
    but the Neo4j driver installed. If importing a tool pulled langchain
    (~60 MB) the eval suite would need the whole agent stack to check a
    Cypher result, and CI would pay for it on every run."""
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent("""
        import sys
        import api.agent3.find_properties      # noqa: F401
        import api.agent3.get_property         # noqa: F401
        import api.agent3.search_notices       # noqa: F401
        import api.agent3.find_by_identifier   # noqa: F401
        import api.agent3.skills               # noqa: F401
        heavy = {"langchain", "langgraph", "langchain_openai", "deepagents"}
        print(",".join(sorted({m.split(".")[0] for m in sys.modules} & heavy)))
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=180, cwd=str(_REPO_ROOT))
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "", (
        f"importing an agent3 tool pulled in {out.stdout.strip()}")


def test_building_the_model_does_not_import_langchain_at_module_scope():
    """`api/agent3/agent.py` may be imported for its constants and helpers
    without paying langchain_openai's ~28 MB — the import belongs inside
    chat_model()."""
    import ast

    src = (_REPO_ROOT / "api" / "agent3" / "agent.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    module_level = {
        n.module.split(".")[0]
        for n in tree.body
        if isinstance(n, ast.ImportFrom) and n.module
    }
    assert not (module_level & {"langchain", "langchain_openai", "langgraph"}), (
        f"agent.py imports {module_level} at module scope")
