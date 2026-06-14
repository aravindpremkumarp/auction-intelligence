"""Tests for dossier Q&A retrieval (pure excerpt logic + the async core).

The agent tool ``query_user_dossier`` is a thin wrapper over
``answer_from_dossier``; testing the core (with the repo faked) covers the
behaviour without standing up a real pydantic-ai agent.
"""
from __future__ import annotations

import asyncio

import pytest

from api.dossier import qa


def test_query_terms_drops_stopwords_and_short_words() -> None:
    terms = qa.query_terms("What does my EC say about a prior mortgage?")
    assert "mortgage" in terms
    assert "ec" in terms
    # Stopwords / short words removed.
    for junk in ("what", "does", "my", "say", "about", "a"):
        assert junk not in terms
    # De-duped, order preserved.
    assert qa.query_terms("mortgage mortgage prior") == ["mortgage", "prior"]


def test_extract_excerpt_windows_around_hit() -> None:
    text = ("X" * 500) + " RESERVE PRICE is twelve lakh rupees " + ("Y" * 500)
    out = qa.extract_excerpt(text, "reserve price", per_doc_chars=400)
    assert "RESERVE PRICE" in out
    assert len(out) <= 420  # bounded (allow small separator slack)
    # Windowed, not the whole doc.
    assert out.count("X") < 500


def test_extract_excerpt_no_hit_returns_head() -> None:
    text = "alpha beta gamma " * 100
    out = qa.extract_excerpt(text, "nonexistentterm", per_doc_chars=50)
    assert out == text[:50]


def test_extract_excerpt_empty_text() -> None:
    assert qa.extract_excerpt("", "anything") == ""


def test_build_matches_ranks_hits_first_and_caps() -> None:
    docs = [
        {"doc_id": "d1", "filename": "tax.pdf", "doc_type": "property_tax_receipt",
         "ocr_text": "property tax paid for 2025"},
        {"doc_id": "d2", "filename": "ec.pdf", "doc_type": "encumbrance_certificate",
         "ocr_text": "this EC records a mortgage in favour of the bank"},
    ]
    matches = qa.build_matches(docs, "mortgage", max_docs=8)
    # The doc that mentions 'mortgage' ranks first.
    assert matches[0]["doc_id"] == "d2"
    assert len(matches) == 2

    # max_docs cap.
    many = [dict(doc_id=str(i), filename=f"f{i}", doc_type="x",
                 ocr_text="no hit here") for i in range(20)]
    assert len(qa.build_matches(many, "mortgage", max_docs=5)) == 5


def test_answer_login_required_when_anonymous() -> None:
    out = asyncio.run(qa.answer_from_dossier(None, "anything"))
    assert out["status"] == "login_required"


def test_answer_no_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.dossier import repository as repo

    async def empty(*args, **kwargs):
        return []

    monkeypatch.setattr(repo, "search_user_documents", empty)
    out = asyncio.run(qa.answer_from_dossier("sub-1", "anything"))
    assert out["status"] == "no_documents"
    assert out["matches"] == []


def test_answer_ok_returns_grounded_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.dossier import repository as repo

    captured: dict = {}

    async def fake_search(sub, *, dossier_id=None, doc_type=None, limit=50):
        captured["sub"] = sub
        captured["dossier_id"] = dossier_id
        captured["doc_type"] = doc_type
        return [{
            "dossier_id": "dos-1", "dossier_title": "Plot 4",
            "property_label": "Plot 4, Avadi", "doc_id": "d2",
            "filename": "ec.pdf", "doc_type": "encumbrance_certificate",
            "category": "D",
            "ocr_text": ("preamble " * 50) + "mortgage created on 2019-04-01 "
                        + ("trailer " * 50),
        }]

    monkeypatch.setattr(repo, "search_user_documents", fake_search)
    out = asyncio.run(qa.answer_from_dossier(
        "sub-1", "any mortgage?", dossier_id="dos-1",
        doc_type="encumbrance_certificate",
    ))
    assert out["status"] == "ok"
    assert out["count"] == 1
    m = out["matches"][0]
    assert m["filename"] == "ec.pdf"
    assert "mortgage created on 2019-04-01" in m["excerpt"]
    # Scope args were forwarded to the ownership-gated repo call.
    assert captured == {"sub": "sub-1", "dossier_id": "dos-1",
                        "doc_type": "encumbrance_certificate"}
