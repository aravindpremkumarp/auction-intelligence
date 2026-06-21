"""
tests/test_dedupe_canonical.py
------------------------------
Unit coverage for the canonical-pick hardening in scripts/dedupe_documents.

The dedupe survivor's ``public_url`` is what the review UI links to, so it must
prefer a node whose R2 object actually exists over one whose URL dangles — the
multi-property-notice 404 class where one notice file is shared across lots but
physically uploaded under a single prefix.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import scripts.dedupe_documents as dd


def _cand(node_id, storage_key=None, has_url=1, has_key=1,
          uploaded_at="", extracted_at=""):
    return {
        "id": node_id,
        "storage_key": storage_key,
        "has_url": has_url,
        "has_key": has_key,
        "uploaded_at": uploaded_at,
        "extracted_at": extracted_at,
    }


def test_pick_canonical_prefers_existing_object(monkeypatch):
    """An older node whose object exists beats a newer one whose URL dangles."""
    present = {"notices/B/x.jpg"}
    monkeypatch.setattr(dd, "_r2_object_exists", lambda k: k in present)
    cands = [
        _cand("A", storage_key="notices/A/x.jpg", uploaded_at="2026-01-02"),
        _cand("B", storage_key="notices/B/x.jpg", uploaded_at="2026-01-01"),
    ]
    canonical, dups = dd._pick_canonical(cands)
    assert canonical == "B"
    assert dups == ["A"]


def test_pick_canonical_falls_back_to_metadata_when_none_exist(monkeypatch):
    """With no object present anywhere, ranking falls back to has_url etc."""
    monkeypatch.setattr(dd, "_r2_object_exists", lambda k: False)
    cands = [
        _cand("A", storage_key="notices/A/x.jpg", has_url=0, uploaded_at="2026-01-01"),
        _cand("B", storage_key="notices/B/x.jpg", has_url=1, uploaded_at="2026-01-01"),
    ]
    canonical, dups = dd._pick_canonical(cands)
    assert canonical == "B"
    assert dups == ["A"]


def test_pick_canonical_newest_wins_when_both_exist(monkeypatch):
    """When both objects exist, the newest uploaded_at is canonical."""
    monkeypatch.setattr(dd, "_r2_object_exists", lambda k: True)
    cands = [
        _cand("A", storage_key="notices/A/x.jpg", uploaded_at="2026-01-01"),
        _cand("B", storage_key="notices/B/x.jpg", uploaded_at="2026-02-01"),
    ]
    canonical, dups = dd._pick_canonical(cands)
    assert canonical == "B"
    assert dups == ["A"]


def test_r2_object_exists_is_best_effort(monkeypatch):
    """A blank key is False; R2 errors degrade to False (never raise)."""
    dd._r2_object_exists.cache_clear()
    assert dd._r2_object_exists("") is False
    assert dd._r2_object_exists(None) is False
