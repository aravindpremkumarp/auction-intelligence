"""Tests for the documents-list dedupe in get_auction_detail (issue #45).

Locks in the API-level safety net: even if the graph briefly contains
duplicate :Document nodes for the same logical file, get_auction_detail
must never return them twice.
"""
from __future__ import annotations


def _patch_detail(monkeypatch, rows: list[dict]) -> None:
    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_read_query",
                        lambda c, p=None, timeout=10.0, max_rows=200: rows)


def test_get_auction_detail_dedupes_documents_by_public_url(monkeypatch) -> None:
    """Two graph rows pointing at the same public_url collapse to one."""
    _patch_detail(monkeypatch, [{
        "fields": {"auction_id": "dup", "title": "Sample"},
        "relationships": {},
        "documents": [
            {
                "filename": "notice.pdf",
                "public_url": "https://r2.example/notices/dup/notice.pdf",
                "content_type": "application/pdf",
                "doc_type": "pdf",
            },
            {
                # Same public_url but content_type drifted — exactly the
                # case where Cypher's `collect(DISTINCT {map})` fails to
                # collapse but our Python dedupe must.
                "filename": "notice.pdf",
                "public_url": "https://r2.example/notices/dup/notice.pdf",
                "content_type": None,
                "doc_type": "pdf",
            },
        ],
        "siblings": [],
    }])
    from api.tools.cypher_tools import get_auction_detail

    out = get_auction_detail("dup")
    assert out is not None
    assert len(out["documents"]) == 1
    assert out["documents"][0]["public_url"] == \
        "https://r2.example/notices/dup/notice.pdf"


def test_get_auction_detail_keeps_distinct_documents(monkeypatch) -> None:
    """Two genuinely-different files (different public_url) both survive."""
    _patch_detail(monkeypatch, [{
        "fields": {"auction_id": "two", "title": "Sample"},
        "relationships": {},
        "documents": [
            {
                "filename": "notice.pdf",
                "public_url": "https://r2.example/notices/two/notice.pdf",
                "content_type": "application/pdf",
                "doc_type": "pdf",
            },
            {
                "filename": "schedule.pdf",
                "public_url": "https://r2.example/notices/two/schedule.pdf",
                "content_type": "application/pdf",
                "doc_type": "pdf",
            },
        ],
        "siblings": [],
    }])
    from api.tools.cypher_tools import get_auction_detail

    out = get_auction_detail("two")
    urls = [d["public_url"] for d in out["documents"]]
    assert urls == [
        "https://r2.example/notices/two/notice.pdf",
        "https://r2.example/notices/two/schedule.pdf",
    ]


def test_get_auction_detail_strips_documents_without_public_url(monkeypatch) -> None:
    """Documents with no public_url (not yet uploaded) are filtered out."""
    _patch_detail(monkeypatch, [{
        "fields": {"auction_id": "partial", "title": "Sample"},
        "relationships": {},
        "documents": [
            {"filename": "ghost.pdf", "public_url": None, "doc_type": "pdf"},
            {
                "filename": "real.pdf",
                "public_url": "https://r2.example/notices/partial/real.pdf",
                "doc_type": "pdf",
            },
        ],
        "siblings": [],
    }])
    from api.tools.cypher_tools import get_auction_detail

    out = get_auction_detail("partial")
    assert len(out["documents"]) == 1
    assert out["documents"][0]["filename"] == "real.pdf"
