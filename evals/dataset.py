"""
evals/dataset.py
----------------
Assembles a `pydantic-evals` Dataset from the golden-question catalogue and
builds the judge model. Importing this module pulls in pydantic-evals (and,
lazily, pydantic-ai), so it is only loaded by the live runner / CI — never by
the lightweight offline shape test.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from pydantic_evals import Case, Dataset

from evals.cases import GOLDEN
from evals.evaluators import NoWriteError, ToolTrajectory, answer_quality_judge


@dataclass
class ChatTaskOutput:
    """What the eval task returns per case: the final answer plus the ordered,
    de-duplicated list of tools the agent invoked on its way there."""

    answer: str
    tools_called: list[str] = field(default_factory=list)


def build_judge_model():
    """OpenRouter-backed judge model, or None to fall back to pydantic-evals'
    default judge model.

    Defaults to the same model the agent uses (``OPENROUTER_MODEL``); override
    with ``EVAL_JUDGE_MODEL`` to grade with a stronger/independent model and
    avoid self-grading bias (e.g. ``anthropic/claude-sonnet-4.5``).
    """
    try:
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        from pipeline.config import (
            OPENROUTER_API_KEY,
            OPENROUTER_BASE_URL,
            OPENROUTER_MODEL,
        )
    except Exception:  # noqa: BLE001 - missing dep/config → use library default
        return None
    if not OPENROUTER_API_KEY:
        return None
    model_id = os.getenv("EVAL_JUDGE_MODEL", OPENROUTER_MODEL)
    provider = OpenAIProvider(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)
    return OpenAIChatModel(model_id, provider=provider)


def build_dataset(include_judge: bool = True) -> Dataset:
    """Build the golden-question Dataset.

    Evaluators are attached at the dataset level so they run on every case.
    `include_judge=False` skips the (paid, slower) LLM-as-judge score — handy
    for a quick tool-trajectory-only check.
    """
    cases = [
        Case(
            name=f"{c.intent}:{c.question[:50]}",
            inputs=c.question,
            metadata={
                "intent": c.intent,
                "acceptable_tools": c.acceptable_tools,
                "must_not_mention_write_error": c.must_not_mention_write_error,
            },
        )
        for c in GOLDEN
    ]
    evaluators = [ToolTrajectory(), NoWriteError()]
    if include_judge:
        evaluators.append(answer_quality_judge(build_judge_model()))
    return Dataset(name="golden-questions", cases=cases, evaluators=evaluators)
