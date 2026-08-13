"""Notice-type -> LangExtract model routing (pure; config-only, no heavy deps).

Kept out of pipeline/load_extractions.py (which imports Neo4j at module load) so
the decision is unit-testable without a database or the `langextract` dependency.

Routing runs on the canonical ``Document.notice_type``: the cluster count (how
many AuctionProperty rows link to the notice) corrected by human review — a
reviewer's override sets ``notice_type_overridden`` and the corrected value
wins. Known limitation: the cluster count is scope-filtered (lots outside Tamil
Nadu, or never scraped, are absent), so an unreviewed multi-lot notice whose
in-scope count collapsed to 1 routes to the cheap single model until a human
corrects it in the classification review queue.
"""
from __future__ import annotations

import os

from pipeline.config import (
    LANGEXTRACT_REASONING_OFF_MODELS,
    OPENROUTER_MODEL_EXTRACT_MULTI,
    OPENROUTER_MODEL_EXTRACT_SINGLE,
)


def char_buffer_for(markdown: str, base: int | None = None,
                    ceil: int | None = None) -> int:
    """LangExtract chunk size (``max_char_buffer``), scaled to notice length.

    LangExtract splits the document into windows of this size and extracts each
    INDEPENDENTLY, so a long multi-lot notice spread over several windows loses
    its global lot numbering — a later window can't see the lots that came
    before it. Sizing the window to the markdown keeps a whole notice in ONE
    window (up to a ceiling), so the model sees every lot at once and can number
    and reason across all of them. Small single notices already fit the base
    window, so they are unchanged.

      base (LANGEXTRACT_MAX_CHAR_BUFFER, default 4000): floor for small notices.
      ceil (LANGEXTRACT_MAX_CHAR_BUFFER_CEILING, default 30000): cap so a
           pathologically long bundle still splits instead of one giant call.
    """
    if base is None:
        base = int(os.environ.get("LANGEXTRACT_MAX_CHAR_BUFFER", "4000"))
    if ceil is None:
        ceil = int(os.environ.get("LANGEXTRACT_MAX_CHAR_BUFFER_CEILING", "30000"))
    return max(base, min(len(markdown or ""), ceil))


def _reasoning_off_substrings() -> list[str]:
    return [s.strip().lower()
            for s in (LANGEXTRACT_REASONING_OFF_MODELS or "").split(",") if s.strip()]


def reasoning_off_for(model_id: str) -> bool:
    """True when this model's provider-side reasoning should be forced off."""
    mid = (model_id or "").lower()
    return any(s in mid for s in _reasoning_off_substrings())


def select_extract_model(notice_type: str | None) -> tuple[str, bool]:
    """Choose the extraction model for one Document.

    Returns ``(model_id, reasoning_off)``. Routes on the canonical
    ``notice_type`` (cluster count + human override; see module docstring),
    defaulting to 'single' when unknown. Only 'multi' picks the multi model —
    any other/unknown label is treated as single (the cheap default).
    """
    label = (notice_type or "single").strip().lower()
    model = (OPENROUTER_MODEL_EXTRACT_MULTI if label == "multi"
             else OPENROUTER_MODEL_EXTRACT_SINGLE)
    return model, reasoning_off_for(model)
