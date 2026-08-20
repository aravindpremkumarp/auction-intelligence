"""
tests/api/test_chat_v2_agents.py
--------------------------------
The off-the-shelf middleware stack, and the two guarantees about it that are
easy to lose to a dependency bump.
"""
from __future__ import annotations

import pytest

from api.chat.v2 import agents


def _names(stack):
    return [getattr(m, "name", type(m).__name__) for m in stack]


def test_stack_is_explicit_and_minimal():
    """Each tier passes its own middleware list, so there is no default stack
    to exclude — and nothing unexpected can ride along."""
    assert _names(agents.model_middleware()) == [
        "ModelRetryMiddleware", "ModelCallLimitMiddleware"]


def test_retry_is_transient_only():
    """Retrying a 4xx just re-fails three times and triples the latency of a
    bad request."""
    retry = agents.model_middleware()[0]
    retry_on = getattr(retry, "retry_on", ())
    assert retry_on, "retry_on must be narrowed, not left at the default Exception"
    assert Exception not in retry_on


def test_tool_middleware_is_deliberately_absent():
    """ToolErrorMiddleware and ToolCallLimitMiddleware only fire inside a graph
    node that executes tools. Ours don't, so their semantics live in
    executor.py — having them here too would be a second implementation."""
    names = _names(agents.model_middleware())
    assert "ToolErrorMiddleware" not in names
    assert "ToolCallLimitMiddleware" not in names


def test_todo_list_middleware_is_rejected_at_build_time():
    """The typed Plan already is the todo list; a second fuzzy copy costs a
    whole model call on a twelve-second turn."""
    class _Fake:
        name = "TodoListMiddleware"

    with pytest.raises(AssertionError, match="TodoListMiddleware"):
        agents.build_tier_agent(system_prompt="x", response_format=None,
                                model_name="flash",
                                extra_middleware=[_Fake()])


def test_model_carries_the_shared_settings(monkeypatch):
    """v2 must not keep its own copy of the model settings: the spike's copy
    was missing provider pinning (which keeps the prompt cache warm) and the
    explicit reasoning-off block (without which V4 thinks by default and burned
    4.5k tokens per planning call)."""
    monkeypatch.setenv("OPENROUTER_CHAT_API_KEY", "sk-test")
    model = agents.chat_model("flash", reasoning_effort="off")
    body = model.extra_body or {}

    assert body.get("usage", {}).get("include") is True
    assert body.get("reasoning") == {"enabled": False}


def test_library_retries_are_off():
    """ModelRetryMiddleware owns retries; the client retrying underneath it
    would multiply the attempts."""
    import os
    os.environ.setdefault("OPENROUTER_CHAT_API_KEY", "sk-test")
    assert agents.chat_model("flash").max_retries == 0


def test_unknown_model_name_falls_back_rather_than_raising():
    import os
    os.environ.setdefault("OPENROUTER_CHAT_API_KEY", "sk-test")
    assert agents.chat_model("nonsense").model_name
