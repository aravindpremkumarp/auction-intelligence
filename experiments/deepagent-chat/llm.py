"""Model factory + usage accounting shared by both variants.

DeepSeek via OpenRouter (same provider/slugs as production). Usage is read
off each AIMessage's `usage_metadata`, so both variants are measured the
same way: model calls = AIMessages produced, tokens = summed metadata.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from langchain_openai import ChatOpenAI

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
FLASH_SLUG = os.getenv("OPENROUTER_MODEL_CHAT_FLASH", "deepseek/deepseek-v4-flash")


def make_model(temperature: float = 0.0) -> ChatOpenAI:
    api_key = os.getenv("OPENROUTER_CHAT_API_KEY") or os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_CHAT_API_KEY / OPENROUTER_API_KEY not set")
    return ChatOpenAI(
        model=FLASH_SLUG,
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        temperature=temperature,
        timeout=90,
        # Same shape production sends (api/model_selection.py): DeepSeek V4 is
        # a hybrid-reasoning model and the provider default is reasoning ON —
        # omitting the block silently burns thinking tokens billed as output.
        # Both variants share this, so the comparison stays fair.
        extra_body={"usage": {"include": True}, "reasoning": {"enabled": False}},
    )


@dataclass
class Usage:
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: list[str] = field(default_factory=list)

    def add_message(self, msg) -> None:
        meta = getattr(msg, "usage_metadata", None)
        self.llm_calls += 1
        if meta:
            self.input_tokens += meta.get("input_tokens", 0) or 0
            self.output_tokens += meta.get("output_tokens", 0) or 0

    def add_from_messages(self, messages) -> None:
        """Sum usage over a LangGraph result: every AIMessage is one model
        call; ToolMessages record which tools ran."""
        for m in messages:
            kind = getattr(m, "type", "")
            if kind == "ai":
                self.add_message(m)
                for tc in getattr(m, "tool_calls", None) or []:
                    self.tool_calls.append(tc.get("name", "?"))
