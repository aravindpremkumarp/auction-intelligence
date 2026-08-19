"""Variant A — the vanilla Deep Agents loop.

`create_deep_agent` with the shared tools and slim instructions, nothing
else. This measures what the harness's default ReAct loop does with our
tool surface: expected to think → tool → think → tool sequentially, i.e.
the same shape that costs production ~5.6 model calls per turn.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm import Usage, make_model  # noqa: E402
from spike_tools import TOOLS, load_instructions  # noqa: E402

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        from deepagents import create_deep_agent

        _agent = create_deep_agent(
            tools=TOOLS,
            system_prompt=load_instructions(),
            model=make_model(),
        )
    return _agent


def run(question: str) -> dict:
    agent = _get_agent()
    t0 = time.perf_counter()
    result = agent.invoke(
        {"messages": [{"role": "user", "content": question}]},
        config={"recursion_limit": 24},
    )
    wall = time.perf_counter() - t0
    usage = Usage()
    messages = result.get("messages", [])
    usage.add_from_messages(messages)
    answer = ""
    for m in reversed(messages):
        if getattr(m, "type", "") == "ai" and m.content:
            answer = m.content if isinstance(m.content, str) else str(m.content)
            break
    return {
        "variant": "A vanilla-deepagents",
        "seconds": round(wall, 1),
        "llm_calls": usage.llm_calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "tools": usage.tool_calls,
        "answer": answer,
    }
