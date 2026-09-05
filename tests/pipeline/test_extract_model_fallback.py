"""The model an unrouted extraction call lands on.

`model_id=None` is not exotic: `load_extractions` and
`reset_langextract_and_extract` both pass None whenever LANGEXTRACT_PROVIDER is
not 'openrouter', and any caller of `langextract_examples.extract` may omit it.
Whatever that resolves to is running extraction against the live corpus, so it
has to be the model the routing config names — not a third model chosen
somewhere else and never measured on this task.
"""
from __future__ import annotations

import pipeline.load_extractions as LD
from pipeline.config import OPENROUTER_MODEL_EXTRACT_SINGLE


def test_an_unrouted_openrouter_call_uses_the_configured_single_model(monkeypatch):
    monkeypatch.delenv("LANGEXTRACT_MODEL_ID", raising=False)
    assert LD._effective_model(None, route=True) == OPENROUTER_MODEL_EXTRACT_SINGLE


def test_the_fallback_is_never_the_chat_default(monkeypatch):
    """`google/gemini-2.5-flash` is the chat agent's and the dossier
    classifier's default. Extraction reaching for it silently overrides a
    measured choice with an unmeasured one."""
    monkeypatch.delenv("LANGEXTRACT_MODEL_ID", raising=False)
    assert LD._effective_model(None, route=True) != "google/gemini-2.5-flash"


def test_an_explicit_model_id_always_wins(monkeypatch):
    monkeypatch.setenv("LANGEXTRACT_MODEL_ID", "someone/else")
    assert LD._effective_model("deepseek/deepseek-v4-pro", route=True) == \
        "deepseek/deepseek-v4-pro"


def test_the_env_override_still_beats_the_configured_default(monkeypatch):
    monkeypatch.setenv("LANGEXTRACT_MODEL_ID", "an/override")
    assert LD._effective_model(None, route=True) == "an/override"


def test_the_gemini_direct_path_keeps_its_own_default(monkeypatch):
    """That path does not go through OpenRouter; its slugs carry no vendor
    prefix and are not interchangeable with the routed ones."""
    monkeypatch.delenv("LANGEXTRACT_MODEL_ID", raising=False)
    assert LD._effective_model(None, route=False) == "gemini-2.5-flash"
