"""
api/chat/v2/agents.py
---------------------
Model and agent construction for the three tiers.

Each tier is a **one-shot, zero-tool, structured-output call**: the planner
emits a `Plan`, the synthesizer emits a `Synthesis`, the composer emits a
`CypherSpec`. Tools are executed by `api/chat/v2/executor.py`, not by an
agent graph.

That shape is the whole point. deepagents' own loop is a ReAct loop — think,
call a tool, think again — which is exactly what /chat v1 already does and
what measured 5.6 sequential model calls per turn. Running the tiers through
`create_agent` rather than a bare `model.invoke()` buys one thing the spike
did not have: `AgentMiddleware` hooks are executed by the agent graph, so the
retry / fallback / call-limit middleware can actually fire.

Model settings come from `api/model_selection.py` — the same function v1
uses. That is deliberate: the spike kept its own copy and was missing
provider pinning, which is what keeps DeepSeek's prompt cache warm, and the
explicit `reasoning: {enabled: false}` block, without which V4 defaults to
reasoning ON via OpenRouter and burned 4.5k thinking tokens per planning call.
"""
from __future__ import annotations

import logging
from typing import Any

from api.model_selection import CHAT_MODEL_SLUGS, build_model_settings
from pipeline.config import OPENROUTER_BASE_URL

logger = logging.getLogger(__name__)

# Hard timeout on one model call. The tiered loop makes 2-3 of them, so this
# has to leave room for all of them inside the request budget.
MODEL_TIMEOUT_S = 90.0


def _api_key() -> str:
    """The chat key, falling back to the shared OpenRouter key — same
    precedence the spike and the pipeline use."""
    import os

    return (
        os.getenv("OPENROUTER_CHAT_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
        or ""
    )


def chat_model(model_name: str, reasoning_effort: str | None = None):
    """A LangChain chat model pointed at OpenRouter, carrying the same
    `extra_body` v1 sends.

    Imported lazily by every caller: `langchain_openai` costs ~28 MB of RSS
    and must not load on a v1-only deploy.
    """
    from langchain_openai import ChatOpenAI

    slug = CHAT_MODEL_SLUGS.get(model_name) or CHAT_MODEL_SLUGS["flash"]
    settings = build_model_settings(reasoning_effort)
    return ChatOpenAI(
        model=slug,
        api_key=_api_key(),
        base_url=OPENROUTER_BASE_URL,
        temperature=0,
        timeout=MODEL_TIMEOUT_S,
        max_retries=0,          # ModelRetryMiddleware owns retries
        extra_body=settings.get("extra_body") or {},
    )


#: Model calls one TIER may make. A tier is a single structured-output call,
#: so anything above this is a runaway, not a hard question.
TIER_RUN_LIMIT = 3


def model_middleware(fallback_to: str | None = None,
                     run_limit: int = TIER_RUN_LIMIT) -> list[Any]:
    """The off-the-shelf stack every tier runs behind.

    `ToolErrorMiddleware` and `ToolCallLimitMiddleware` are absent on purpose:
    they only fire inside a graph node that executes tools, and these agents
    have no tools. Their semantics live in `executor.py`, where the calls
    actually run, so the behaviour exists once instead of twice.

    `run_limit` is a parameter because `api/chat/deep` shares this stack and a
    ReAct loop is a different shape: the spike measured a multi-hop question at
    9 model calls inside ONE graph run, where a tier is 1 by construction.
    Leaving the tier's limit on the deep loop would `error` out every hard
    question and score the A/B against a loop that was never allowed to
    finish.
    """
    import httpx
    from langchain.agents.middleware import (
        ModelCallLimitMiddleware,
        ModelRetryMiddleware,
    )

    stack: list[Any] = [
        # Transient provider failure only — never a 4xx, which would just
        # re-fail three times and triple the latency of a bad request.
        ModelRetryMiddleware(
            max_retries=2,
            retry_on=(httpx.TimeoutException, httpx.NetworkError,
                      httpx.RemoteProtocolError),
            backoff_factor=2.0,
            initial_delay=1.0,
        ),
        # `error` so a runaway surfaces instead of silently returning a
        # partial answer.
        ModelCallLimitMiddleware(run_limit=run_limit, exit_behavior="error"),
    ]
    if fallback_to:
        # Emergency path only. A flash->pro hop changes per-turn cost AND
        # busts the prompt-cache prefix for that turn, so it must be loud.
        stack.insert(1, _LoudFallback(fallback_to))
    return stack


def _LoudFallback(model_name: str):  # noqa: N802 - reads as a constructor
    from langchain.agents.middleware import ModelFallbackMiddleware

    class LoudFallback(ModelFallbackMiddleware):
        def __init__(self, target):
            super().__init__(target)

        def wrap_model_call(self, request, handler):  # type: ignore[override]
            try:
                return super().wrap_model_call(request, handler)
            finally:
                logger.warning(
                    "chat v2: model fallback stack engaged (target=%s) — "
                    "per-turn cost and prompt-cache prefix both change",
                    model_name,
                )

    return LoudFallback(chat_model(model_name))


def build_tier_agent(
    *,
    system_prompt: str,
    response_format: Any,
    model_name: str,
    reasoning_effort: str | None = None,
    fallback_to: str | None = None,
    extra_middleware: list[Any] | None = None,
):
    """One tier's agent: no tools, structured output, explicit middleware."""
    from langchain.agents import create_agent

    stack = model_middleware(fallback_to) + list(extra_middleware or [])
    _assert_no_todo_list(stack)
    return create_agent(
        model=chat_model(model_name, reasoning_effort),
        tools=[],
        system_prompt=system_prompt,
        response_format=response_format,
        middleware=stack,
    )


def _assert_no_todo_list(stack: list[Any]) -> None:
    """`TodoListMiddleware` must never reach a chat tier.

    The planner's typed `Plan` already IS the todo list; a second fuzzy copy
    costs a whole model call on a turn that is supposed to take twelve
    seconds. It is not a `create_agent` default today — this asserts that a
    dependency bump has not made it one, and that nothing appends it by
    accident.
    """
    names = {getattr(m, "name", type(m).__name__) for m in stack}
    assert "TodoListMiddleware" not in names, (
        "TodoListMiddleware reached a chat tier — it duplicates the typed "
        "plan at the cost of a model call. Keep it for the check agent."
    )
