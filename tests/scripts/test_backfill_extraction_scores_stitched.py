"""The score backfill validates stored entities against their source text. A
stitched leader's entities index the joined text, so it must read that text,
and a follower's stale extraction is not scored at all."""
from __future__ import annotations

import scripts.backfill_extraction_scores as B


class _Capture:
    def __init__(self):
        self.cypher = None

    def __call__(self, cypher, params=None, **kw):
        self.cypher = cypher
        return []


def test_backfill_scores_against_the_stitched_text_and_skips_followers(monkeypatch):
    for force in (False, True):
        cap = _Capture()
        monkeypatch.setattr(B, "run_read_query", cap)
        B.load_unscored(force)
        assert "coalesce(d.stitched_markdown, d.markdown) AS md" in cap.cypher
        assert "d.stitched_into IS NULL" in cap.cypher
