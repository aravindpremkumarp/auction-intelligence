"""
tests/api/test_reingest_engine.py
---------------------------------
The annotator's engine selector must reach whole-notice re-ingest.

It used to reach only the per-block re-extract path. `reingest_notice` took no
engine argument at all ("Re-run the full MinerU pipeline"), the router never
passed one, and the frontend POSTed an empty body — so picking Datalab in the
toolbar still sent every re-ingest to MinerU. When the MinerU key expired, the
background task 401'd and the UI spun forever on a 202 it could not walk back.

Covers the contract, not the OCR: the engine reaches the background task, the
provenance stamp follows the engine instead of being hardcoded to mineru, and
an unknown engine degrades to the default rather than 400ing a long-running job.
"""
from __future__ import annotations

import importlib
import inspect

import pytest

from api.review import blocks as B

# `from api.review import router` yields the APIRouter instance, not the
# module — api/review/__init__.py re-exports it under the same name.
R = importlib.import_module("api.review.router")


# ── _norm_engine ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("datalab", "datalab"),
    ("DataLab", "datalab"),
    ("  datalab  ", "datalab"),
    ("mineru", "mineru"),
    ("MinerU", "mineru"),
])
def test_norm_engine_accepts_real_engines(raw, expected):
    assert B._norm_engine(raw) == expected


@pytest.mark.parametrize("raw", ["tesseract", "", None, "  "])
def test_norm_engine_degrades_unknown_to_default(raw):
    """An engine string is a display preference arriving from the annotator.
    A typo must not 400 a job that takes minutes — it falls back."""
    assert B._norm_engine(raw) == "mineru"


def test_norm_engine_honours_explicit_default():
    assert B._norm_engine(None, default="datalab") == "datalab"


# ── signatures: the engine has to be threadable end to end ──────────────────

@pytest.mark.parametrize("fn", [
    B.reingest_notice,
    B.reingest_notice_safe,
    B._reingest_multi_region,
])
def test_reingest_chain_accepts_engine(fn):
    assert "engine" in inspect.signature(fn).parameters, (
        f"{fn.__name__} drops the engine — the selector stops here and the "
        "call below it silently reverts to MinerU"
    )


def test_multi_region_accepts_datalab_mode():
    """Datalab tiers by notice_type (single->fast, multi->accurate); the
    multi-region path must be able to pass that through."""
    assert "mode" in inspect.signature(B._reingest_multi_region).parameters


def test_router_endpoint_accepts_a_body():
    """The endpoint must take a body to carry `engine`. It previously had
    none, so the frontend had nowhere to put the selection."""
    assert "body" in inspect.signature(R.review_notice_reingest).parameters


# ── provenance stamping ─────────────────────────────────────────────────────

def _capture_persist(monkeypatch):
    captured = {}

    def fake_run_query(_cypher, params):
        captured.update(params)
        return []

    monkeypatch.setattr(B, "run_query", fake_run_query)
    return captured


def test_persist_stamps_datalab_provenance(monkeypatch):
    """Regression: markdown_source/model were hardcoded to mineru, so a
    Datalab re-ingest branded its own output as MinerU — misrouting every
    downstream consumer that reads provenance."""
    captured = _capture_persist(monkeypatch)
    B._persist_reingest_result(
        "n.jpg", markdown="# x", blocks_json="{}", markdown_raw="# x",
        blocks_raw="[]", engine="datalab", model="datalab-accurate",
    )
    assert captured["source"] == "datalab"
    assert captured["model"] == "datalab-accurate"


def test_persist_defaults_to_mineru(monkeypatch):
    captured = _capture_persist(monkeypatch)
    B._persist_reingest_result(
        "n.jpg", markdown="# x", blocks_json="{}", markdown_raw="# x",
        blocks_raw="[]",
    )
    assert captured["source"] == "mineru"
    assert captured["model"] == "mineru-vlm"


def test_persist_derives_model_from_engine(monkeypatch):
    """Callers that pass an engine but no model still get honest provenance."""
    captured = _capture_persist(monkeypatch)
    B._persist_reingest_result(
        "n.jpg", markdown="# x", blocks_json="{}", markdown_raw="# x",
        blocks_raw="[]", engine="datalab",
    )
    assert captured["source"] == "datalab"
    assert captured["model"].startswith("datalab")


def test_persist_rejects_bogus_engine_in_stamp(monkeypatch):
    """A bad engine must never be written as provenance."""
    captured = _capture_persist(monkeypatch)
    B._persist_reingest_result(
        "n.jpg", markdown="# x", blocks_json="{}", markdown_raw="# x",
        blocks_raw="[]", engine="tesseract",
    )
    assert captured["source"] == "mineru"
