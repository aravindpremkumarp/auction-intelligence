"""scripts.collapse_identical_documents: the rule that decides what may be deleted.

Network-free — both functions under test are pure, and they are the two that
decide whether a Document is destroyed, so they are the ones worth pinning.
"""
from __future__ import annotations

import pytest

from scripts.collapse_identical_documents import _rank, collapsible


def _doc(auction: str, name: str, sha: str | None, *, rels: int = 5,
         props: int = 40, uploaded: str | None = "2026-08-08T10:00:00Z") -> dict:
    return {"auction_id": auction, "id": f"id-{name}", "filename": name,
            "public_url": f"https://r2/{name}", "uploaded_at": uploaded,
            "rels": rels, "props": props, "lots": 1, "sha": sha}


def test_two_copies_on_one_auction_are_collapsible():
    docs = [_doc("A1", "a.jpg", "sha1"), _doc("A1", "b.jpg", "sha1")]
    groups = collapsible(docs)
    assert len(groups) == 1
    auction, sha, group = groups[0]
    assert (auction, sha) == ("A1", "sha1")
    assert {d["filename"] for d in group} == {"a.jpg", "b.jpg"}


def test_a_multi_property_notice_is_never_collapsible():
    """The KARNTK case: one notice, six auctions, six real lots.

    Identical bytes across *different* auctions is the normal shape of a notice
    advertising several properties. Collapsing it would delete five auctions'
    extractions, so it must not even be offered.
    """
    docs = [_doc(f"A{i}", f"KARNTK{i}.jpg", "sha1") for i in range(6)]
    assert collapsible(docs) == []


def test_two_different_files_on_one_auction_are_not_duplicates():
    """A notice and its corrigendum both hang off one auction. Both are real."""
    docs = [_doc("A1", "notice.jpg", "sha1"), _doc("A1", "corrigendum.jpg", "sha2")]
    assert collapsible(docs) == []


def test_a_document_that_could_not_be_hashed_is_left_alone():
    docs = [_doc("A1", "a.jpg", None), _doc("A1", "b.jpg", None)]
    assert collapsible(docs) == []


def test_mixed_corpus_yields_only_the_safe_group():
    docs = [
        _doc("A1", "dup-1.png", "shaX"), _doc("A1", "dup-2.png", "shaX"),
        _doc("A2", "multi-1.jpg", "shaY"), _doc("A3", "multi-2.jpg", "shaY"),
        _doc("A4", "notice.jpg", "shaZ"), _doc("A4", "corrigendum.jpg", "shaW"),
    ]
    assert [(a, sha) for a, sha, _ in collapsible(docs)] == [("A1", "shaX")]


def test_the_richer_node_survives():
    poor = _doc("A1", "poor.png", "sha1", rels=4)
    rich = _doc("A1", "rich.png", "sha1", rels=5)
    assert max([poor, rich], key=_rank) is rich


def test_properties_break_a_relationship_tie():
    thin = _doc("A1", "thin.png", "sha1", rels=5, props=40)
    full = _doc("A1", "full.png", "sha1", rels=5, props=48)
    assert max([thin, full], key=_rank) is full


def test_the_original_beats_the_re_upload():
    """Equal in every other way, the earlier upload is the one that was meant."""
    first = _doc("A1", "first.png", "sha1", uploaded="2026-08-08T17:56:30.131Z")
    again = _doc("A1", "again.png", "sha1", uploaded="2026-08-08T17:56:31.007Z")
    assert max([first, again], key=_rank) is first


def test_a_missing_timestamp_never_wins_on_seniority():
    """No recorded upload is not evidence of being the original."""
    dated = _doc("A1", "dated.png", "sha1", uploaded="2026-08-08T17:56:31.007Z")
    undated = _doc("A1", "undated.png", "sha1", uploaded=None)
    assert max([dated, undated], key=_rank) is dated


@pytest.mark.parametrize("docs", [[], [_doc("A1", "only.jpg", "sha1")]])
def test_nothing_to_collapse(docs):
    assert collapsible(docs) == []
