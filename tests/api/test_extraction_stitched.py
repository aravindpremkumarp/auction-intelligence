"""The review API on a stitched notice: leader shows the joined text, follower
points at the leader, and the stale badge sees the classification stamp."""
from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException

from api.review import extraction as E


def test_rerun_worker_reads_the_stitched_text_and_refuses_followers():
    src = inspect.getsource(E._rerun_worker)
    assert "coalesce(d.stitched_markdown, d.markdown) AS md" in src
    assert "d.stitched_into AS stitched_into" in src
    assert "stitched into" in src   # the refusal message


# ── stale badge sees the classification / stitch stamp ──────────────────────

def test_stale_when_the_stamp_is_newer_than_the_extraction():
    assert E.extraction_stale(None, None, "2026-09-05T17:43:51Z",
                              extraction_stale_at="2026-09-05T18:10:17Z") is True


def test_not_stale_when_the_stamp_is_older_than_the_extraction():
    assert E.extraction_stale(None, None, "2026-09-05T18:20:00Z",
                              extraction_stale_at="2026-09-05T18:10:17Z") is False


def test_stamp_is_ignored_without_an_extraction_time():
    assert E.extraction_stale(None, None, None,
                              extraction_stale_at="2026-09-05T18:10:17Z") is False


# ── queries ─────────────────────────────────────────────────────────────────

def test_queue_filter_excludes_followers():
    assert "d.stitched_into IS NULL" in E._extraction_filter_clause(
        None, None, None, None, None, None, None)


def test_queue_query_returns_the_stamp_and_the_group_count():
    src = inspect.getsource(E.list_extraction_queue)
    assert "toString(d.extraction_stale_at) AS extraction_stale_at" in src
    assert ("coalesce(d.stitched_expected_lot_count, d.expected_lot_count) "
            "AS expected_lot_count") in src
    assert "coalesce(d.stitched_pages, [d.filename]) AS stitched_pages" in src


def test_detail_query_returns_the_stitched_text_and_layout():
    src = inspect.getsource(E.get_extraction)
    # substring checks only — the query aligns its AS columns with padding
    assert "coalesce(d.stitched_markdown, d.markdown)" in src
    assert "d.stitched_pages" in src and "d.stitched_page_offsets" in src
    assert "d.stitched_into" in src
    assert "toString(d.extraction_stale_at)" in src


# ── follower detail ─────────────────────────────────────────────────────────

def test_follower_detail_points_at_the_leader(monkeypatch):
    monkeypatch.setattr(E, "get_extraction", lambda fn: None)
    monkeypatch.setattr(E, "get_stitch_pointer", lambda fn: "AXIS-1.jpg")
    out = E.extraction_detail("AXIS-2.jpg", _admin=object())
    assert out.stitched_into == "AXIS-1.jpg"
    assert out.fields == []


def test_follower_with_an_old_extraction_still_points_at_the_leader(monkeypatch):
    row = {"filename": "AXIS-2.jpg", "markdown": "old", "extraction_json": "[]",
           "corrections_json": "{}", "status": "verified", "stitched_into": "AXIS-1.jpg"}
    monkeypatch.setattr(E, "get_extraction", lambda fn: row)
    out = E.extraction_detail("AXIS-2.jpg", _admin=object())
    assert out.stitched_into == "AXIS-1.jpg"
    assert out.fields == []


def test_leader_detail_carries_pages_offsets_and_stale(monkeypatch):
    row = {"filename": "AXIS-1.jpg", "markdown": "P1\n\nP2", "extraction_json": "[]",
           "corrections_json": "{}", "status": "pending", "stitched_into": None,
           "stitched_pages": ["AXIS-1.jpg", "AXIS-2.jpg"],
           "stitched_page_offsets": [0, 4],
           "extraction_at": "2026-09-05T17:43:51Z",
           "extraction_stale_at": "2026-09-05T18:10:17Z"}
    monkeypatch.setattr(E, "get_extraction", lambda fn: row)
    out = E.extraction_detail("AXIS-1.jpg", _admin=object())
    assert out.stitched_pages == ["AXIS-1.jpg", "AXIS-2.jpg"]
    assert out.stitched_page_offsets == [0, 4]
    assert out.stale is True


# ── write endpoints refuse a follower ────────────────────────────────────────

def test_edit_field_refuses_a_follower(monkeypatch):
    monkeypatch.setattr(E, "get_stitch_pointer", lambda fn: "AXIS-1.jpg")

    def _recorder(*a, **kw):
        pytest.fail("save_field_correction must not be called on a follower")

    monkeypatch.setattr(E, "save_field_correction", _recorder)
    body = E.FieldEditBody(field_id="0", value="x")
    with pytest.raises(HTTPException) as e:
        E.extraction_edit_field("AXIS-2.jpg", body, admin=object())
    assert e.value.status_code == 409


def test_verify_refuses_a_follower(monkeypatch):
    monkeypatch.setattr(E, "get_stitch_pointer", lambda fn: "AXIS-1.jpg")

    def _recorder(*a, **kw):
        pytest.fail("verify_extraction must not be called on a follower")

    monkeypatch.setattr(E, "verify_extraction", _recorder)
    body = E.ExtractionVerifyBody()
    with pytest.raises(HTTPException) as e:
        E.extraction_verify("AXIS-2.jpg", body, admin=object())
    assert e.value.status_code == 409


def test_unverify_refuses_a_follower(monkeypatch):
    monkeypatch.setattr(E, "get_stitch_pointer", lambda fn: "AXIS-1.jpg")

    def _recorder(*a, **kw):
        pytest.fail("unverify_extraction must not be called on a follower")

    monkeypatch.setattr(E, "unverify_extraction", _recorder)
    with pytest.raises(HTTPException) as e:
        E.extraction_unverify("AXIS-2.jpg", admin=object())
    assert e.value.status_code == 409
