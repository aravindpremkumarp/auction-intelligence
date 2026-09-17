"""Unit tests for the spot-check audit endpoints (api/review/spotcheck.py).

Pure logic against monkeypatched query helpers — no DB and no app, the way
tests/api/test_review_extraction.py does it.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

import api.review.spotcheck as sc
from pipeline.spotcheck import CORRECT, UNCLEAR, WRONG


class _Admin:
    email = "admin@example.com"


def _ents(n=6):
    return [
        {"id": str(i), "cls": "location", "text": f"Village {i}",
         "start": i * 30, "end": i * 30 + 9,
         "attrs": {"village": f"V{i}", "lot_index": "1"}}
        for i in range(n)
    ]


def _extractions(*filenames, n=6):
    """Stand-in for `_fetch_extractions`: payloads keyed by filename."""
    return {fn: json.dumps(_ents(n)) for fn in filenames}


def _doc_row(fn="a.pdf", stale_at=None, n=6):
    """A population row — identifiers and timestamps only, no extraction JSON.

    Mirrors the real split: `_fetch_population` lists the scope cheaply and
    `_fetch_extractions` fetches payloads for the sampled documents alone.
    """
    return {
        "filename": fn,
        "extraction_at": "2026-09-01T00:00:00Z",
        "markdown_reextracted_at": None,
        "markdown_loaded_at": None,
        "extraction_stale_at": stale_at,
    }


# ── drawing a sample ─────────────────────────────────────────────────────────
def test_create_sample_freezes_items_and_stores_the_seed(monkeypatch):
    written = {}
    monkeypatch.setattr(sc, "_fetch_population",
                        lambda scope: [_doc_row("a.pdf"), _doc_row("b.pdf")])
    monkeypatch.setattr(sc, "_fetch_extractions",
                        lambda fns: _extractions(*fns))
    monkeypatch.setattr(sc, "run_query",
                        lambda cy, params: written.update(params) or [])

    out = sc.create_sample(
        sc.SpotCheckCreateBody(size=6, seed=99), "admin@example.com")

    assert out["seed"] == 99
    assert out["size"] == 6
    items = json.loads(written["items"])
    assert len(items) == 6
    # Frozen with everything needed to review it later, including the passage
    # to highlight — not just a pointer that could drift.
    assert {"key", "filename", "field_id", "cls", "attr", "value",
            "span_text", "stratum", "question"} <= set(items[0])
    # The scope travels with the sample: a precision figure is uninterpretable
    # without knowing which population it describes.
    assert json.loads(written["scope"]) is not None
    assert written["seed"] == 99


def test_same_seed_redraws_the_identical_sample(monkeypatch):
    monkeypatch.setattr(sc, "_fetch_population",
                        lambda scope: [_doc_row("a.pdf"), _doc_row("b.pdf")])
    monkeypatch.setattr(sc, "_fetch_extractions",
                        lambda fns: _extractions(*fns))
    seen = []
    monkeypatch.setattr(sc, "run_query",
                        lambda cy, params: seen.append(params) or [])

    body = sc.SpotCheckCreateBody(size=5, seed=7)
    sc.create_sample(body, "a@b.c")
    sc.create_sample(body, "a@b.c")

    first = [i["key"] for i in json.loads(seen[0]["items"])]
    second = [i["key"] for i in json.loads(seen[1]["items"])]
    assert first == second      # an audit that can't be redrawn can't be checked


def test_stale_documents_are_excluded_and_counted(monkeypatch):
    # b.pdf's markdown was rewritten after extraction: its spans describe text
    # that no longer exists, so it cannot be judged against what's on screen.
    monkeypatch.setattr(sc, "_fetch_population", lambda scope: [
        _doc_row("a.pdf"),
        _doc_row("b.pdf", stale_at="2026-09-09T00:00:00Z"),
    ])
    monkeypatch.setattr(sc, "_fetch_extractions",
                        lambda fns: _extractions(*fns))
    monkeypatch.setattr(sc, "run_query", lambda cy, params: [])

    out = sc.create_sample(sc.SpotCheckCreateBody(size=10, seed=1), "a@b.c")
    assert out["excluded_stale"] == 1
    assert out["documents_in_pool"] == 1


def test_extractions_are_fetched_only_for_the_sampled_documents(monkeypatch):
    """The expensive payload query must not run over the whole corpus.

    Listing the population is cheap (ids + timestamps); pulling every
    extraction_json is not. Stale documents are dropped before the payload
    fetch, so they are never paid for either.
    """
    asked = {}
    monkeypatch.setattr(sc, "_fetch_population", lambda scope: [
        _doc_row("a.pdf"),
        _doc_row("stale.pdf", stale_at="2026-09-09T00:00:00Z"),
    ])
    monkeypatch.setattr(sc, "_fetch_extractions",
                        lambda fns: asked.update(fns=list(fns)) or _extractions(*fns))
    monkeypatch.setattr(sc, "run_query", lambda cy, params: [])

    sc.create_sample(sc.SpotCheckCreateBody(size=4, seed=1), "a@b.c")
    assert asked["fns"] == ["a.pdf"]


def test_population_query_has_no_limit_that_would_bias_the_draw(monkeypatch):
    # A Cypher LIMIT over `ORDER BY d.filename` would make every sample "the
    # alphabetically first N documents" — a biased sample dressed up as a
    # random one. The seeded choice has to happen over the whole population.
    sent = {}
    monkeypatch.setattr(sc, "run_read_query",
                        lambda cy, params, **kw: sent.update(cypher=cy) or [])
    sc._fetch_population(sc.SpotCheckScope())
    body = sent["cypher"].upper()
    assert "LIMIT" not in body
    assert "ORDER BY" in body        # deterministic order for the seeded draw


def test_scope_clause_does_not_filter_on_review_status():
    clause = sc._scope_clause(sc.SpotCheckScope(notice_type="multi"))
    # Auditing only unverified documents could never catch a bulk verify that
    # was clicked through without reading — the exact failure this queue exists
    # to detect.
    assert "extraction_review_status" not in clause
    assert "notice_type" in clause


def test_scope_clause_carries_score_and_batch_filters():
    clause = sc._scope_clause(sc.SpotCheckScope(score_min=50, batch=7))
    assert "$score_min" in clause and "$batch" in clause


# ── verdicts ─────────────────────────────────────────────────────────────────
def _loaded(items=None, verdicts=None):
    items = items if items is not None else [
        {"key": "a.pdf#0#@span", "filename": "a.pdf", "field_id": "0",
         "cls": "location", "attr": None, "value": "Village 0",
         "span_text": "Village 0", "start": 0, "end": 9,
         "stratum": "cls:location", "question": "this text is a location",
         "lot_index": "1"},
        {"key": "a.pdf#1#village", "filename": "a.pdf", "field_id": "1",
         "cls": "location", "attr": "village", "value": "V1",
         "span_text": "Village 1", "start": 30, "end": 39,
         "stratum": "attr:village", "question": "village = 'V1'",
         "lot_index": "1"},
    ]
    return {"id": "s1", "created_at": "2026-09-17T00:00:00Z",
            "created_by": "a@b.c", "seed": 5, "closed_at": None,
            "scope": {"batch": 3}, "items": items,
            "verdicts": verdicts or {}, "pool_documents": 2,
            "excluded_stale": 0}


def test_verdict_is_recorded_with_reviewer_and_time(monkeypatch):
    saved = {}
    monkeypatch.setattr(sc, "_load", lambda sid: _loaded())
    monkeypatch.setattr(sc, "run_query",
                        lambda cy, params: saved.update(params) or [])

    out = sc.spotcheck_verdict(
        "s1", sc.VerdictBody(key="a.pdf#0#@span", verdict=CORRECT), _Admin())

    rec = json.loads(saved["v"])["a.pdf#0#@span"]
    assert rec["verdict"] == CORRECT
    assert rec["by"] == "admin@example.com"
    assert rec["at"]
    assert out.judged == 1 and out.pending == 1


def test_verdict_on_a_key_outside_the_sample_is_refused(monkeypatch):
    monkeypatch.setattr(sc, "_load", lambda sid: _loaded())
    monkeypatch.setattr(sc, "run_query", lambda cy, params: [])
    with pytest.raises(HTTPException) as e:
        sc.spotcheck_verdict(
            "s1", sc.VerdictBody(key="other.pdf#9#@span", verdict=CORRECT),
            _Admin())
    # Accepting it would widen the denominator of a frozen sample.
    assert e.value.status_code == 400


def test_unknown_verdict_word_is_refused(monkeypatch):
    monkeypatch.setattr(sc, "_load", lambda sid: _loaded())
    monkeypatch.setattr(sc, "run_query", lambda cy, params: [])
    with pytest.raises(HTTPException) as e:
        sc.spotcheck_verdict(
            "s1", sc.VerdictBody(key="a.pdf#0#@span", verdict="looks_fine"),
            _Admin())
    assert e.value.status_code == 400


def test_verdict_overwrites_rather_than_duplicating(monkeypatch):
    saved = {}
    existing = {"a.pdf#0#@span": {"verdict": WRONG, "by": "x", "at": "t"}}
    monkeypatch.setattr(sc, "_load", lambda sid: _loaded(verdicts=existing))
    monkeypatch.setattr(sc, "run_query",
                        lambda cy, params: saved.update(params) or [])

    sc.spotcheck_verdict(
        "s1", sc.VerdictBody(key="a.pdf#0#@span", verdict=CORRECT), _Admin())
    stored = json.loads(saved["v"])
    assert stored["a.pdf#0#@span"]["verdict"] == CORRECT
    assert len(stored) == 1


# ── next item ────────────────────────────────────────────────────────────────
def _ctx(**over):
    base = {"context": "Village 0 and more text", "context_start": 0,
            "context_end": 9, "public_url": "https://x/a.png",
            "doc_type": "image", "stale": False, "anchor": "stored"}
    base.update(over)
    return base


def test_next_returns_the_first_unjudged_item(monkeypatch):
    monkeypatch.setattr(sc, "_load", lambda sid: _loaded(
        verdicts={"a.pdf#0#@span": {"verdict": CORRECT}}))
    monkeypatch.setattr(sc, "_context_for", lambda it: _ctx())

    out = sc.spotcheck_next("s1", at=None, _admin=_Admin())
    assert out.key == "a.pdf#1#village"
    assert out.index == 2 and out.total == 2
    assert out.question == "village = 'V1'"


def test_next_with_at_steps_back_to_a_judged_item(monkeypatch):
    monkeypatch.setattr(sc, "_load", lambda sid: _loaded(
        verdicts={"a.pdf#0#@span": {"verdict": WRONG, "note": "wrong village"}}))
    monkeypatch.setattr(sc, "_context_for", lambda it: _ctx())

    out = sc.spotcheck_next("s1", at="a.pdf#0#@span", _admin=_Admin())
    assert out.key == "a.pdf#0#@span"
    assert out.verdict == WRONG          # the earlier answer comes back with it
    assert out.note == "wrong village"


def test_next_on_a_finished_sample_reports_complete(monkeypatch):
    monkeypatch.setattr(sc, "_load", lambda sid: _loaded(verdicts={
        "a.pdf#0#@span": {"verdict": CORRECT},
        "a.pdf#1#village": {"verdict": UNCLEAR}}))
    with pytest.raises(HTTPException) as e:
        sc.spotcheck_next("s1", at=None, _admin=_Admin())
    assert e.value.status_code == 404
    assert "complete" in e.value.detail


def test_next_surfaces_a_drifted_anchor_instead_of_hiding_it(monkeypatch):
    monkeypatch.setattr(sc, "_load", lambda sid: _loaded())
    monkeypatch.setattr(sc, "_context_for",
                        lambda it: _ctx(stale=True, anchor="fuzzy"))
    out = sc.spotcheck_next("s1", at=None, _admin=_Admin())
    # Judging a drifted span is how an audit records a verdict about the wrong
    # text, so the reviewer has to be told.
    assert out.stale is True and out.anchor == "fuzzy"


# ── report ───────────────────────────────────────────────────────────────────
def test_report_carries_draw_provenance(monkeypatch):
    monkeypatch.setattr(sc, "_load", lambda sid: _loaded(verdicts={
        "a.pdf#0#@span": {"verdict": CORRECT},
        "a.pdf#1#village": {"verdict": WRONG}}))
    rep = sc.spotcheck_report("s1", _admin=_Admin())
    assert rep["excluded_stale"] == 0
    assert rep["pool_documents"] == 2
    assert rep["seed"] == 5
    assert "batch=3" in rep["scope"]
    # Two graded items is under MIN_N, so no rate is claimed.
    assert rep["overall"]["precision"] is None
    assert rep["overall"]["n"] == 2
