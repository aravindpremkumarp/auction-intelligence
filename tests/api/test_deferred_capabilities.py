"""
tests/api/test_deferred_capabilities.py
---------------------------------------
Guards the pydantic-ai v2 deferred-capability wiring in api/agent.py: the
long-tail tools (`run_cypher`, `describe_schema`, `internet_search`) ride
behind on-demand capabilities so their schemas stay out of the always-sent
prompt prefix, and are revealed by the framework's `load_capability` tool.

conftest replaces `api.agent` in sys.modules with a stub (so importing
api.main never builds the real OpenRouter client), so this file loads the
real module under an alias via importlib. The graph/tool layer underneath is
the conftest neo4j stub, and every run here uses TestModel/FunctionModel —
no network.

TestModel is representative of production visibility: like the OpenRouter
DeepSeek models (OpenAI Chat Completions API), it has no native tool-search
surface, so pydantic-ai drops undiscovered deferred tools from the wire and
exposes the local `load_capability` / `search_tools` framework tools instead.
(FunctionModel is NOT representative of visibility — its profile claims
native tool search, so deferred tools stay on the wire flagged for the
provider to hide — but its load/replay mechanics are identical, so it drives
the scripted load_capability turns below.)
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

_REPO_ROOT = Path(__file__).resolve().parents[2]

CORE_TOOLS = {"search_auctions", "semantic_search", "get_auction_detail"}
DEFERRED_TOOLS = {"run_cypher", "describe_schema", "internet_search"}


def _real_agent_module():
    """The real api/agent.py, loaded under an alias past the conftest stub."""
    if "api_agent_real" in sys.modules:
        return sys.modules["api_agent_real"]
    spec = importlib.util.spec_from_file_location(
        "api_agent_real", _REPO_ROOT / "api" / "agent.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["api_agent_real"] = mod
    spec.loader.exec_module(mod)
    return mod


def _visible_tools(message_history=None) -> set[str]:
    """Run one no-tool-call turn on TestModel and return the function-tool
    names that reached the model request."""
    mod = _real_agent_module()
    model = TestModel(call_tools=[])
    asyncio.run(
        mod.agent.run(
            "hello", deps=mod.ChatDeps(), model=model,
            message_history=message_history,
        )
    )
    return {t.name for t in model.last_model_request_parameters.function_tools}


def _run_with_cypher_load():
    """One agent run whose first model response loads the `cypher`
    capability, scripted via FunctionModel."""
    mod = _real_agent_module()

    def scripted(messages, info):
        if len(messages) == 1:  # first request of the run
            return ModelResponse(
                parts=[ToolCallPart(tool_name="load_capability", args={"id": "cypher"})]
            )
        return ModelResponse(parts=[TextPart("done")])

    return asyncio.run(
        mod.agent.run(
            "novel graph question", deps=mod.ChatDeps(),
            model=FunctionModel(scripted),
        )
    )


def test_deferred_tools_hidden_on_first_request():
    visible = _visible_tools()
    assert CORE_TOOLS <= visible, f"core tools missing from {visible}"
    assert "load_capability" in visible, "framework load tool not exposed"
    assert not (DEFERRED_TOOLS & visible), (
        f"deferred tools leaked into the always-sent surface: "
        f"{DEFERRED_TOOLS & visible}"
    )


def test_catalog_rides_in_instructions():
    mod = _real_agent_module()
    model = TestModel(call_tools=[])
    result = asyncio.run(mod.agent.run("hello", deps=mod.ChatDeps(), model=model))
    instructions = result.all_messages()[0].instructions or ""
    for cap_id in ("cypher", "web-search"):
        assert cap_id in instructions, (
            f"capability {cap_id!r} missing from the deferred catalog"
        )


def test_loaded_capability_survives_history_replay():
    """A stored conversation that loaded `cypher` must resume with the cypher
    tools visible — pydantic-ai reconstructs loaded state from the
    load_capability call/return pairs in history (which is why the router's
    trim pass must never touch them)."""
    first = _run_with_cypher_load()
    visible = _visible_tools(message_history=first.all_messages())
    assert {"run_cypher", "describe_schema"} <= visible, (
        f"cypher tools not restored from history: {visible}"
    )
    # The web-search capability was never loaded and must stay hidden.
    assert "internet_search" not in visible


def test_load_capability_returns_cypher_instructions():
    result = _run_with_cypher_load()
    returns = [
        p
        for m in result.all_messages()
        for p in m.parts
        if getattr(p, "part_kind", "") == "tool-return"
        and p.tool_name == "load_capability"
    ]
    assert returns, "no load_capability tool return in history"
    text = str(returns[0].content)
    # The Cypher rules moved out of modes/_shared.md live here now.
    assert "ZONED DATETIME" in text
    assert "describe_schema" in text
