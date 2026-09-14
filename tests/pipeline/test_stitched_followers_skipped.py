"""A follower (page 2 of a stitched notice) holds no extraction of its own that
apply or promote should act on: its lots live on the leader. Source-level
guards on the two fetch queries."""
from __future__ import annotations

import inspect

import pipeline.apply_extractions as AX
import pipeline.promote_extractions as P


def test_apply_fetch_skips_followers():
    assert "d.stitched_into IS NULL" in inspect.getsource(AX.fetch_work)


def test_promote_fetch_skips_followers():
    assert "d.stitched_into IS NULL" in P._FETCH
