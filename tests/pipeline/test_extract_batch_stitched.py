"""load_from_neo4j (pipeline/extract_batch.py) is a second LX.extract entry
point, alongside pipeline/load_extractions.py and
scripts/reset_langextract_and_extract.py. It must follow the same stitch
contract: skip a follower (`d.stitched_into IS NULL`) and read the leader's
joined text (`coalesce(d.stitched_markdown, d.markdown) AS md`).

pipeline.extract_batch imports pipeline.langextract_examples eagerly at
module load time, and that module raises ModuleNotFoundError when the
`langextract` package isn't installed (true in this environment — see
tests/scripts/test_reset_langextract_refresh.py's `_write_cypher` for the
same workaround). A stub module standing in for it, installed via
monkeypatch before the import, lets `pipeline.extract_batch` import
successfully; the query-building code under test never touches it.
"""
from __future__ import annotations

import sys
import types

import api.neo4j_client as neo4j_client


class _Capture:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.cypher = None

    def __call__(self, cypher, params=None, **kw):
        self.cypher = cypher
        return self.rows


def test_load_from_neo4j_skips_followers_and_reads_stitched_text(monkeypatch):
    stub = types.ModuleType("pipeline.langextract_examples")
    stub.extract = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "pipeline.langextract_examples", stub)
    monkeypatch.delitem(sys.modules, "pipeline.extract_batch", raising=False)
    import pipeline.extract_batch as EB

    cap = _Capture()
    monkeypatch.setattr(neo4j_client, "run_read_query", cap)
    EB.load_from_neo4j(None)
    assert "d.stitched_into IS NULL" in cap.cypher
    assert "coalesce(d.stitched_markdown, d.markdown) AS md" in cap.cypher
