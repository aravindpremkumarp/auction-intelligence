"""Guard: a stored extraction_score always carries the validators.py scale that
produced it (extraction_score_version), so a penalty change can't silently
rewrite the meaning of every historical score.

Pure tests — run_query / run_read_query are stubbed, no DB, no LLM.
"""
from __future__ import annotations

import sys

from pipeline.validators import SCORE_VERSION, validate, validate_stored


def test_validate_reports_the_scale_it_scored_on():
    """Both entry points hand back the version beside the score, so a caller
    persisting one has the other without importing the constant itself."""
    for report in (validate([], source_text=""),
                   validate_stored([], source_text="")):
        assert report["score_version"] == SCORE_VERSION


def test_extract_write_stamps_the_current_version():
    """pipeline.load_extractions._extract_one persists score + score version in
    the same SET, so the two can never drift apart."""
    import pipeline.load_extractions as le

    captured = {}

    def fake_run_query(cypher, params=None, **kw):
        captured["cypher"] = cypher
        captured["params"] = params or {}
        return [{"d.filename": "n.pdf"}]

    class _Res:
        extractions = []

    class _LX:
        @staticmethod
        def extract(md, **kw):
            return _Res()

    orig = le.run_query
    try:
        le.run_query = fake_run_query
        ok, _, log = le._extract_one({"filename": "n.pdf", "md": "text"},
                                     batch=7, route=False, LX=_LX)
    finally:
        le.run_query = orig

    assert ok, log
    assert "d.extraction_score_version = $score_version" in captured["cypher"]
    assert captured["params"]["score_version"] == SCORE_VERSION


def test_copied_extraction_keeps_the_donors_version():
    """A twin copies the donor's entities, so it must copy the scale they were
    scored on too — stamping this run's version would claim a re-levelling that
    never happened, and a pre-versioning donor must stay unversioned so the
    backfill picks the copy up with it."""
    import pipeline.load_extractions as le

    captured = {}

    def fake_run_query(cypher, params=None, **kw):
        captured["cypher"] = cypher
        captured["params"] = params or {}
        return [{"n": 1}]

    orig = le.run_query
    try:
        le.run_query = fake_run_query
        le._copy_extraction(
            {"filename": "donor.pdf", "j": "[]", "score": 61,
             "score_version": None, "model": "m"},
            ["twin.pdf"], batch=3)
    finally:
        le.run_query = orig

    assert "d.extraction_score_version = $score_version" in captured["cypher"]
    assert captured["params"]["score_version"] is None
    assert captured["params"]["score"] == 61


def test_backfill_selects_unscored_and_stale_versions():
    """The default scope is "no score OR scored on an older scale"; --force drops
    the version bound entirely. coalesce(...,0) is what makes a pre-versioning
    score count as stale rather than current."""
    import scripts.backfill_extraction_scores as bf

    captured = {}

    def fake_read(cypher, params=None, **kw):
        captured["cypher"] = cypher
        captured["params"] = params or {}
        return []

    orig = bf.run_read_query
    try:
        bf.run_read_query = fake_read
        bf.load_unscored(force=False)
        assert "d.extraction_score IS NULL" in captured["cypher"]
        assert ("coalesce(d.extraction_score_version, 0) < $version"
                in captured["cypher"])
        assert captured["params"]["version"] == SCORE_VERSION

        bf.load_unscored(force=True)
        assert "extraction_score_version" not in captured["cypher"]
    finally:
        bf.run_read_query = orig


def test_backfill_write_stamps_the_version(monkeypatch):
    """Rescoring re-levels a document, so the write must move its version
    forward — otherwise the next run finds the same documents stale forever."""
    import scripts.backfill_extraction_scores as bf

    captured = {}
    monkeypatch.setattr(bf, "run_read_query", lambda *a, **k: [
        {"filename": "n.pdf", "md": "text", "ej": "[]"}])
    monkeypatch.setattr(bf, "run_query", lambda cypher, params=None, **k:
                        captured.update(cypher=cypher, params=params or {}) or [])
    monkeypatch.setattr(sys, "argv", ["backfill_extraction_scores"])

    assert bf.main() == 0
    assert "d.extraction_score_version = $version" in captured["cypher"]
    assert captured["params"]["version"] == SCORE_VERSION


def test_score_vs_review_scopes_to_one_scale_by_default():
    """Pooling scores from different penalty tables compares two scales, so the
    report defaults to the current one and only --all-versions widens it."""
    import scripts.score_vs_review as sv

    captured = {}

    def fake_read(cypher, params=None, **kw):
        captured["cypher"] = cypher
        captured["params"] = params or {}
        return []

    orig = sv.run_read_query
    try:
        sv.run_read_query = fake_read
        sv.load_reviewed(all_versions=False)
        assert ("coalesce(d.extraction_score_version, 0) = $version"
                in captured["cypher"])
        assert captured["params"]["version"] == SCORE_VERSION

        sv.load_reviewed(all_versions=True)
        assert "extraction_score_version" not in captured["cypher"]
    finally:
        sv.run_read_query = orig


def test_spearman_reads_order_not_magnitude():
    """The calibration number: a score that perfectly predicts less reviewer work
    is -1, an unrelated one is near 0. Ties (the 0-score floor) must not crash it."""
    scores = [0.0, 10.0, 40.0, 70.0, 90.0, 100.0]
    edits = [0.9, 0.8, 0.6, 0.3, 0.1, 0.0]          # perfectly monotone
    assert sv_spearman(scores, edits) == -1.0

    # Ties (many documents on the 0-score floor) average their ranks instead of
    # crashing, and cost the correlation some strength.
    tied = [0.9, 0.9, 0.6, 0.3, 0.1, 0.0]
    assert -1.0 < sv_spearman(scores, tied) < -0.9

    flat = [0.5] * len(scores)
    assert sv_spearman(scores, flat) is None        # no variation -> no signal
    assert sv_spearman([1.0, 2.0], [1.0, 2.0]) is None   # too few points


def sv_spearman(xs, ys):
    import scripts.score_vs_review as sv
    return sv._spearman(xs, ys)
