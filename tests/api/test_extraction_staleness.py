"""The extraction staleness rule (api/review/extraction.extraction_stale).

An extraction is stale when the markdown was re-ingested after LangExtract last
ran — a full MinerU re-ingest stamps markdown_loaded_at, a single-block re-OCR
stamps markdown_reextracted_at, so EITHER being newer than extraction_at means
the stored fields/offsets no longer match the source and a re-run is required.
"""
from __future__ import annotations

from api.review.extraction import extraction_stale

BEFORE = "2026-07-15T09:00:00.000000000+00:00"
EXTRACTED = "2026-07-15T12:00:00.000000000+00:00"
AFTER = "2026-07-15T13:00:00.000000000+00:00"


def test_fresh_when_markdown_predates_extraction():
    assert extraction_stale(BEFORE, BEFORE, EXTRACTED) is False


def test_stale_when_block_reextracted_after_extraction():
    assert extraction_stale(AFTER, None, EXTRACTED) is True


def test_stale_when_full_reingest_after_extraction():
    assert extraction_stale(None, AFTER, EXTRACTED) is True


def test_stale_if_either_marker_is_newer():
    # full re-ingest newer even though the block marker is older
    assert extraction_stale(BEFORE, AFTER, EXTRACTED) is True


def test_fresh_when_no_markdown_change_markers():
    assert extraction_stale(None, None, EXTRACTED) is False


def test_fresh_when_extraction_time_unknown():
    # legacy row with no extraction_at — nothing to compare against
    assert extraction_stale(AFTER, AFTER, None) is False
