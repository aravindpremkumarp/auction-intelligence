"""A capped run hands the machine back without losing a page.

`--max-seconds` exists for the scheduled runner (render.yaml's auction-extract
cron): it stops STARTING documents past the cap so two firings never work the
same queue. Pages it never started keep no extraction, so the next run selects
them again.

DB-free: the fetch, the planner and the extract call are stubbed.
"""
from __future__ import annotations

import pipeline.load_extractions as M


def _docs(n):
    return [{"filename": f"{i}.jpg", "md": f"TEXT {i}", "twins": [f"{i}.jpg"]}
            for i in range(n)]


def _harness(monkeypatch, docs, extracted, clock):
    monkeypatch.setattr(M, "_fetch", lambda *a, **k: list(docs))
    monkeypatch.setattr(M, "_next_batch", lambda: 1)
    monkeypatch.setattr(M, "_plan_groups", lambda d, **k: (list(d), 0))
    monkeypatch.setattr(M, "time", clock)

    def fake_extract_one(d, batch, route, LX):
        extracted.append(d["filename"])
        clock.advance(10)
        return True, "m", f"{d['filename']}: ok"

    monkeypatch.setattr(M, "_extract_one", fake_extract_one)
    import sys
    import types
    monkeypatch.setitem(sys.modules, "pipeline.langextract_examples",
                        types.ModuleType("pipeline.langextract_examples"))


class _Clock:
    """monotonic() that only moves when a document is extracted."""

    def __init__(self): self.now = 0.0

    def monotonic(self): return self.now

    def advance(self, s): self.now += s


def test_the_cap_stops_starting_work_and_leaves_the_rest_pending(monkeypatch, capsys):
    extracted: list = []
    _harness(monkeypatch, _docs(10), extracted, _Clock())
    assert M.run(None, False, None, workers=1, max_seconds=25) == 0
    # 3 documents fit under a 25s cap at 10s each; the rest were never started
    assert len(extracted) == 3
    out = capsys.readouterr().out
    assert "7 page(s) not started" in out
    assert "the next run picks them up" in out


def test_without_a_cap_every_page_is_started(monkeypatch, capsys):
    extracted: list = []
    _harness(monkeypatch, _docs(10), extracted, _Clock())
    assert M.run(None, False, None, workers=1) == 0
    assert len(extracted) == 10
    assert "not started" not in capsys.readouterr().out


def test_a_cap_larger_than_the_work_changes_nothing(monkeypatch, capsys):
    extracted: list = []
    _harness(monkeypatch, _docs(4), extracted, _Clock())
    assert M.run(None, False, None, workers=1, max_seconds=10_000) == 0
    assert len(extracted) == 4
    assert "not started" not in capsys.readouterr().out
