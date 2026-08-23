"""Offline guards for LangExtract per-notice model routing (pipeline/extract_routing).

Pure: imports only pipeline.extract_routing (-> pipeline.config), no Neo4j and no
`langextract`. Proves the routing decision — which the load_extractions batch
applies per Document — sends single vs multi notices to the right model based on
the canonical (cluster-count + human-override) notice_type.
"""
from __future__ import annotations

import pytest

from pipeline import extract_routing as er
from pipeline.config import (
    OPENROUTER_MODEL_EXTRACT_MULTI as MULTI,
    OPENROUTER_MODEL_EXTRACT_SINGLE as SINGLE,
)


def _model(notice_type):
    return er.select_extract_model(notice_type)[0]


# ── routing by label ──────────────────────────────────────────────────────────
def test_multi_routes_to_multi_model():
    assert _model("multi") == MULTI


def test_single_routes_to_single_model():
    assert _model("single") == SINGLE


def test_defaults_to_single_when_nothing_known():
    assert _model(None) == SINGLE


@pytest.mark.parametrize("label", ["", "  ", "unknown", "MULTI-ish", None])
def test_only_exact_multi_picks_multi(label):
    # Any label that isn't exactly "multi" (case/space-normalised) is treated as
    # single — the cheap default — so a garbled label never over-spends.
    assert _model(label) == SINGLE


def test_label_is_case_and_space_insensitive():
    assert _model("  Multi ") == MULTI


# ── reasoning-off flag ────────────────────────────────────────────────────────
def test_reasoning_off_matches_configured_substrings(monkeypatch):
    monkeypatch.setattr(er, "LANGEXTRACT_REASONING_OFF_MODELS", "deepseek,qwen")
    assert er.reasoning_off_for("deepseek/deepseek-v4-pro") is True
    assert er.reasoning_off_for("QWEN/qwen-max") is True          # case-insensitive
    assert er.reasoning_off_for("tencent/hy3-preview") is False
    assert er.reasoning_off_for("google/gemini-2.5-flash") is False


def test_reasoning_off_empty_config_disables_override(monkeypatch):
    monkeypatch.setattr(er, "LANGEXTRACT_REASONING_OFF_MODELS", "")
    assert er.reasoning_off_for("deepseek/deepseek-v4-pro") is False


def test_select_returns_consistent_reasoning_flag(monkeypatch):
    # the bool select_extract_model returns must equal reasoning_off_for(model).
    monkeypatch.setattr(er, "LANGEXTRACT_REASONING_OFF_MODELS", "deepseek")
    for nt in ("single", "multi", None):
        model, off = er.select_extract_model(nt)
        assert off is er.reasoning_off_for(model)


def test_reasoning_on_for_both_by_default(monkeypatch):
    # Shipped default: empty suppression list -> reasoning stays ON (provider
    # default) for both models; neither single nor multi is forced off.
    monkeypatch.setattr(er, "LANGEXTRACT_REASONING_OFF_MODELS", "")
    assert er.select_extract_model("single")[1] is False
    assert er.select_extract_model("multi")[1] is False


# ── dynamic chunk size ────────────────────────────────────────────────────────
def test_char_buffer_scales_with_markdown():
    # small notice -> base floor (one window, unchanged); medium -> the WHOLE
    # notice in one window (so lots don't split across independent chunks); huge
    # bundle -> capped at the ceiling so it still splits.
    assert er.char_buffer_for("x" * 500, base=4000, ceil=30000) == 4000
    assert er.char_buffer_for("x" * 12000, base=4000, ceil=30000) == 12000
    assert er.char_buffer_for("x" * 50000, base=4000, ceil=30000) == 30000
    assert er.char_buffer_for("", base=4000, ceil=30000) == 4000


def test_char_buffer_reads_env(monkeypatch):
    monkeypatch.setenv("LANGEXTRACT_MAX_CHAR_BUFFER", "5000")
    monkeypatch.setenv("LANGEXTRACT_MAX_CHAR_BUFFER_CEILING", "20000")
    assert er.char_buffer_for("x" * 100) == 5000       # below floor -> floor
    assert er.char_buffer_for("x" * 12000) == 12000    # between -> whole notice
    assert er.char_buffer_for("x" * 25000) == 20000    # above ceiling -> ceiling
