"""Offline guards for LangExtract per-notice model routing (pipeline/extract_routing).

Pure: imports only pipeline.extract_routing (-> pipeline.config), no Neo4j and no
`langextract`. Proves the routing decision — which the load_extractions batch
applies per Document — sends single vs multi notices to the right model and, most
importantly, routes on the classifier's markdown-based verdict over the
cluster-count notice_type.
"""
from __future__ import annotations

import pytest

from pipeline import extract_routing as er
from pipeline.config import (
    OPENROUTER_MODEL_EXTRACT_MULTI as MULTI,
    OPENROUTER_MODEL_EXTRACT_SINGLE as SINGLE,
)


def _model(notice_type, classifier_pred):
    return er.select_extract_model(notice_type, classifier_pred)[0]


# ── routing by label ──────────────────────────────────────────────────────────
def test_multi_prediction_routes_to_multi_model():
    assert _model(None, "multi") == MULTI


def test_single_prediction_routes_to_single_model():
    assert _model(None, "single") == SINGLE


def test_classifier_pred_overrides_cluster_notice_type():
    # THE point of routing on the classifier: a notice tagged 'single' by cluster
    # count (only 1 in-scope AuctionProperty) but seen as 'multi' from the markdown
    # must get the multi model — else a long multi-lot notice gets the cheap one.
    assert _model("single", "multi") == MULTI
    # and the converse: cluster 'multi' but classifier 'single' -> single model.
    assert _model("multi", "single") == SINGLE


def test_falls_back_to_notice_type_when_no_prediction():
    assert _model("multi", None) == MULTI
    assert _model("single", None) == SINGLE


def test_defaults_to_single_when_nothing_known():
    assert _model(None, None) == SINGLE


@pytest.mark.parametrize("label", ["", "  ", "unknown", "MULTI-ish", None])
def test_only_exact_multi_picks_multi(label):
    # Any label that isn't exactly "multi" (case/space-normalised) is treated as
    # single — the cheap default — so a garbled prediction never over-spends.
    assert _model(None, label) == SINGLE


def test_label_is_case_and_space_insensitive():
    assert _model(None, "  Multi ") == MULTI


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
    for nt, pred in [("single", None), ("multi", None), (None, "multi")]:
        model, off = er.select_extract_model(nt, pred)
        assert off is er.reasoning_off_for(model)


def test_reasoning_on_for_both_by_default(monkeypatch):
    # Shipped default: empty suppression list -> reasoning stays ON (provider
    # default) for both models; neither single nor multi is forced off.
    monkeypatch.setattr(er, "LANGEXTRACT_REASONING_OFF_MODELS", "")
    assert er.select_extract_model(None, "single")[1] is False
    assert er.select_extract_model(None, "multi")[1] is False


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
