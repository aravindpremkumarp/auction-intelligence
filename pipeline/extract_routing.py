"""Notice-type -> LangExtract model routing (pure; config-only, no heavy deps).

Kept out of pipeline/load_extractions.py (which imports Neo4j at module load) so
the decision is unit-testable without a database or the `langextract` dependency.

Why route on the CLASSIFIER'S verdict, not notice_type
------------------------------------------------------
`Document.notice_type` is set from the CLUSTER COUNT — how many AuctionProperty
rows link to the notice — which is a scope-filtered subset: lots outside Tamil
Nadu, or present in the source notice but never scraped, are simply absent. So a
genuinely multi-lot notice whose in-scope count collapses to 1 is tagged
'single'. The extractor, though, runs on the full MinerU markdown (which still
holds all the lots), so routing it by cluster count would send a long multi-lot
document to the cheap single-notice model.

`notice_type_classifier_pred` is the LLM's verdict from reading that same
markdown (pipeline/classify_notice.py), so it matches what the extractor sees.
We route on it first and fall back to notice_type only when it is absent.
"""
from __future__ import annotations

from pipeline.config import (
    LANGEXTRACT_REASONING_OFF_MODELS,
    OPENROUTER_MODEL_EXTRACT_MULTI,
    OPENROUTER_MODEL_EXTRACT_SINGLE,
)


def _reasoning_off_substrings() -> list[str]:
    return [s.strip().lower()
            for s in (LANGEXTRACT_REASONING_OFF_MODELS or "").split(",") if s.strip()]


def reasoning_off_for(model_id: str) -> bool:
    """True when this model's provider-side reasoning should be forced off."""
    mid = (model_id or "").lower()
    return any(s in mid for s in _reasoning_off_substrings())


def select_extract_model(notice_type: str | None,
                         classifier_pred: str | None) -> tuple[str, bool]:
    """Choose the extraction model for one Document.

    Returns ``(model_id, reasoning_off)``. Routes on the classifier's
    markdown-based verdict when present (see module docstring), else the
    cluster-count notice_type, else 'single'. Only 'multi' picks the multi
    model — any other/unknown label is treated as single (the cheap default).
    """
    label = (classifier_pred or notice_type or "single").strip().lower()
    model = (OPENROUTER_MODEL_EXTRACT_MULTI if label == "multi"
             else OPENROUTER_MODEL_EXTRACT_SINGLE)
    return model, reasoning_off_for(model)
