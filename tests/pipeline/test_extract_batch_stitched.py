"""load_from_neo4j and load_from_graph (pipeline/extract_batch.py) read notice
text alongside pipeline/load_extractions.py and
scripts/reset_langextract_and_extract.py. They must follow the same stitch
contract: skip a follower (`d.stitched_into IS NULL`) and read the leader's
joined text (`coalesce(d.stitched_markdown, d.markdown) AS md`).

pipeline.extract_batch imports pipeline.langextract_examples eagerly at
module load time, and that module raises ModuleNotFoundError when the
`langextract` package isn't installed (true in this environment — see
tests/scripts/test_reset_langextract_refresh.py's `_write_cypher` for the
same workaround). A stub module standing in for it lets
`pipeline.extract_batch` import; the query-building code under test never
touches it. The fixture puts `sys.modules` and the `pipeline` package back as
it found them, so the module built on the stub cannot leak into later tests.
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest

import api.neo4j_client as neo4j_client

_NAME = "pipeline.extract_batch"


class _Capture:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.cypher = None

    def __call__(self, cypher, params=None, **kw):
        self.cypher = cypher
        return self.rows


@pytest.fixture
def EB(monkeypatch):
    import pipeline
    stub = types.ModuleType("pipeline.langextract_examples")
    stub.extract = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "pipeline.langextract_examples", stub)
    had_mod, old_mod = _NAME in sys.modules, sys.modules.pop(_NAME, None)
    had_attr = hasattr(pipeline, "extract_batch")
    old_attr = getattr(pipeline, "extract_batch", None)
    try:
        yield importlib.import_module(_NAME)
    finally:
        if had_mod:
            sys.modules[_NAME] = old_mod
        else:
            sys.modules.pop(_NAME, None)
        if had_attr:
            pipeline.extract_batch = old_attr
        elif hasattr(pipeline, "extract_batch"):
            delattr(pipeline, "extract_batch")


def test_load_from_neo4j_skips_followers_and_reads_stitched_text(EB, monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(neo4j_client, "run_read_query", cap)
    EB.load_from_neo4j(None)
    assert "d.stitched_into IS NULL" in cap.cypher
    assert "coalesce(d.stitched_markdown, d.markdown) AS md" in cap.cypher


def test_load_from_graph_skips_followers_and_reads_stitched_text(EB, monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(neo4j_client, "run_read_query", cap)
    EB.load_from_graph(None)
    assert "d.stitched_into IS NULL" in cap.cypher
    assert "coalesce(d.stitched_markdown, d.markdown) AS md" in cap.cypher
