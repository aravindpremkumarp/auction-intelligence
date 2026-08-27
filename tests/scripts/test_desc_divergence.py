"""Unit tests for the sibling-aware description divergence check.

Everything under test is pure — no Neo4j, no network. The fixtures mimic the
shape the check exists for: sibling flats in one building whose notice and
portal texts are near-identical apart from a door number.
"""
from __future__ import annotations

from scripts.desc_divergence import (
    absolute_overlap,
    assess_notice,
    bucket,
    match_score,
    shared_tokens,
    tokenize,
)

# Two flats in one block: everything matches but the door number.
_COMMON = "flat in sri towers survey no 45 2b thiruvanmiyur chennai"


def _listing(aid: str, notice: str, portal: str) -> dict:
    return {"aid": aid, "notice": notice, "portal": portal}


# ── tokens ───────────────────────────────────────────────────────────────────

def test_tokenize_keeps_numbers_and_drops_single_chars():
    assert tokenize("Door No. 12A, Flat 3") == {"door", "no", "12a", "flat"}


def test_tokenize_handles_none_and_empty():
    assert tokenize(None) == set()
    assert tokenize("") == set()


def test_shared_tokens_with_two_siblings_is_the_intersection():
    a, b = {"survey", "45", "door", "12"}, {"survey", "45", "door", "14"}
    assert shared_tokens([a, b]) == {"survey", "45", "door"}


def test_shared_tokens_needs_more_than_half_not_at_least_half():
    # With two siblings, "at least half" would call every token shared and
    # erase the door numbers that separate them.
    a, b = {"x"}, {"y"}
    assert shared_tokens([a, b]) == set()


def test_shared_tokens_sheds_boilerplate_missing_from_one_bad_ocr_pass():
    sets = [{"village", "a"}, {"village", "b"}, {"c"}]
    assert shared_tokens(sets) == {"village"}


def test_shared_tokens_needs_two_sets():
    assert shared_tokens([{"a", "b"}]) == set()


# ── scoring ──────────────────────────────────────────────────────────────────

def test_match_score_is_the_portal_side_fraction():
    assert match_score({"12a", "x"}, {"12a", "y"}) == 0.5


def test_match_score_of_empty_portal_never_wins():
    assert match_score({"12a"}, set()) == 0.0


def test_absolute_overlap_is_jaccard():
    assert absolute_overlap("alpha beta", "beta gamma") == round(1 / 3, 4)


def test_absolute_overlap_is_the_shared_pipeline_function():
    """The report and the write-time guard must score tokens identically —
    a report that disagreed with the gate it informs would be worse than none."""
    from pipeline.text_overlap import description_overlap
    assert absolute_overlap is description_overlap


def test_bucket_boundaries():
    assert bucket(0.9) == "similar"
    assert bucket(0.5) == "similar"
    assert bucket(0.3) == "moderate"
    assert bucket(0.1) == "different"
    assert bucket(0.05) == "very_different"


# ── the actual check ─────────────────────────────────────────────────────────

def test_correctly_assigned_siblings_are_ok():
    rows = assess_notice([
        _listing("A", f"{_COMMON} door no 12", f"{_COMMON} door no 12"),
        _listing("B", f"{_COMMON} door no 14", f"{_COMMON} door no 14"),
    ])
    assert {r["auction_id"]: r["verdict"] for r in rows} == {"A": "ok", "B": "ok"}


def test_swapped_descriptions_are_flagged_misassigned():
    # A carries B's notice text and vice versa — the exact wrong-lot error.
    rows = assess_notice([
        _listing("A", f"{_COMMON} door no 14", f"{_COMMON} door no 12"),
        _listing("B", f"{_COMMON} door no 12", f"{_COMMON} door no 14"),
    ])
    by_aid = {r["auction_id"]: r for r in rows}
    assert by_aid["A"]["verdict"] == "misassigned"
    assert by_aid["A"]["best_sibling_id"] == "B"
    assert by_aid["B"]["verdict"] == "misassigned"


def test_the_old_absolute_check_would_have_missed_the_swap():
    # The whole reason this script changed: siblings share so much text that a
    # swapped pair still looks "similar" on plain overlap.
    rows = assess_notice([
        _listing("A", f"{_COMMON} door no 14", f"{_COMMON} door no 12"),
        _listing("B", f"{_COMMON} door no 12", f"{_COMMON} door no 14"),
    ])
    assert all(r["bucket"] == "similar" for r in rows)
    assert all(r["verdict"] == "misassigned" for r in rows)


def _partial_lead() -> list[dict]:
    """A's notice text points at B, but only covers half of B's distinguishing
    portal tokens — a 0.50 lead rather than the 1.00 of a clean swap."""
    return [
        _listing("A", f"{_COMMON} door no 14", f"{_COMMON} door no 12"),
        _listing("B", f"{_COMMON} door no 12", f"{_COMMON} door no 14 15"),
    ]


def test_a_lead_inside_the_margin_is_close_not_misassigned():
    rows = {r["auction_id"]: r for r in assess_notice(_partial_lead(), margin=0.60)}
    assert rows["A"]["lead"] == 0.50
    assert rows["A"]["verdict"] == "close"


def test_the_same_lead_outside_the_margin_is_misassigned():
    rows = {r["auction_id"]: r for r in assess_notice(_partial_lead(), margin=0.40)}
    assert rows["A"]["lead"] == 0.50
    assert rows["A"]["verdict"] == "misassigned"


def test_identical_siblings_are_indistinguishable_not_flagged():
    rows = assess_notice([
        _listing("A", _COMMON, _COMMON),
        _listing("B", _COMMON, _COMMON),
    ])
    assert {r["verdict"] for r in rows} == {"indistinguishable"}


def test_single_listing_falls_back_to_the_absolute_buckets():
    rows = assess_notice([_listing("A", _COMMON, _COMMON)])
    assert rows[0]["verdict"] == "no_siblings"
    assert rows[0]["bucket"] == "similar"


def test_single_listing_with_unrelated_texts_buckets_as_very_different():
    rows = assess_notice([_listing("A", _COMMON, "vacant land in madurai east")])
    assert rows[0]["verdict"] == "no_siblings"
    assert rows[0]["bucket"] == "very_different"


def test_missing_text_is_reported_not_scored():
    rows = assess_notice([
        _listing("A", f"{_COMMON} door no 12", f"{_COMMON} door no 12"),
        _listing("B", "", f"{_COMMON} door no 14"),
    ])
    by_aid = {r["auction_id"]: r for r in rows}
    assert by_aid["B"]["verdict"] == "no_text"
    # A is now the only usable listing, so it has no sibling to compare against.
    assert by_aid["A"]["verdict"] == "no_siblings"


def test_a_wordier_sibling_blurb_does_not_win_by_length_alone():
    # B's portal text is padded with boilerplate; normalising by the portal
    # side is what stops that padding from beating A's own match.
    padding = " ".join(f"term{i}" for i in range(40))
    rows = assess_notice([
        _listing("A", f"{_COMMON} door no 12", f"{_COMMON} door no 12"),
        _listing("B", f"{_COMMON} door no 14", f"{_COMMON} door no 14 {padding}"),
    ])
    assert {r["auction_id"]: r["verdict"] for r in rows} == {"A": "ok", "B": "ok"}


def test_every_listing_gets_exactly_one_row():
    listings = [
        _listing("A", f"{_COMMON} door no 12", f"{_COMMON} door no 12"),
        _listing("B", f"{_COMMON} door no 14", f"{_COMMON} door no 14"),
        _listing("C", "", ""),
    ]
    rows = assess_notice(listings)
    assert sorted(r["auction_id"] for r in rows) == ["A", "B", "C"]
