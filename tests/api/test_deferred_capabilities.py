"""
tests/api/test_deferred_capabilities.py
---------------------------------------
Guards the pydantic-ai v2 deferred-capability wiring in api/agent.py: the
raw-Cypher tools (`run_cypher`, `describe_schema`) ride behind the on-demand
`cypher` capability so their schemas stay out of the always-sent prompt
prefix, and are revealed by the framework's `load_capability` tool. Only the
rarely-used Cypher tools are deferred — `internet_search` is used often
enough that the per-load round-trip would cost more than it saves, so it
stays always-on (see api/agent.py).

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

# internet_search is deliberately always-on (not deferred) — see module docstring.
CORE_TOOLS = {"search_auctions", "semantic_search", "get_auction_detail", "internet_search"}
DEFERRED_TOOLS = {"run_cypher", "describe_schema"}


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
    assert "cypher" in instructions, "cypher capability missing from the deferred catalog"
    # web-search is always-on, not a deferred capability — it must NOT appear
    # as a catalog entry.
    assert "web-search" not in instructions


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
    # internet_search is always-on, so it is visible on every request whether
    # or not any capability has been loaded.
    assert "internet_search" in visible


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
