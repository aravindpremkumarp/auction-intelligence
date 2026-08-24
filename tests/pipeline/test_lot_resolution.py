"""pipeline.lot_resolution: which lot on a multi-lot notice a listing is.

Every case is shaped after the live example that motivated this module:
auction 796269, a 6-lot Sriperumbudur notice where the listing's reserve
price (₹4,14,79,000) matches exactly one lot, and only that one.
"""
from __future__ import annotations

from pipeline.lot_resolution import (
    RESULT_BORROWER, RESULT_RESERVE, RESULT_RESERVE_BORROWER, lot_match_key,
    resolve_lot,
)

_CANDIDATES = [
    {"lot_key": "notice.jpg#1", "reserve": 4160000,
     "borrowers": ["Mr. Karthikeyan Selvakumar"]},
    {"lot_key": "notice.jpg#2", "reserve": 8355000,
     "borrowers": ["Mr M. Dhamodharan"]},
    {"lot_key": "notice.jpg#3", "reserve": 41479000,
     "borrowers": ["M/s. Sri Vaaru Traders, Represented by its Proprietor "
                  "Mr. Maneesh A P", "Mr. Prakasam D"]},
    {"lot_key": "notice.jpg#4", "reserve": 28950000,
     "borrowers": ["Mr. Saravanan P"]},
    {"lot_key": "notice.jpg#5", "reserve": 8830000,
     "borrowers": ["Mrs. S Rajalakshmi"]},
    {"lot_key": "notice.jpg#6", "reserve": 11500000,
     "borrowers": ["Mr. Sabha Khan"]},
]


def test_a_unique_reserve_match_resolves_alone():
    """The live case: reserve price alone, no borrower needed."""
    out = resolve_lot(listing_reserve=41479000, listing_borrower=None,
                      candidates=_CANDIDATES)
    assert out["lot_key"] == "notice.jpg#3"
    assert out["method"] == RESULT_RESERVE


def test_reserve_match_ignores_float_noise_from_parsing():
    out = resolve_lot(listing_reserve=41479000.00000001, listing_borrower=None,
                      candidates=_CANDIDATES)
    assert out["lot_key"] == "notice.jpg#3"


def test_a_shorter_borrower_name_matches_the_lot_s_longer_party_string():
    """The listing carries "M/s. Sri Vaaru Traders"; the lot's party list
    carries the fuller legal string that CONTAINS it. token_set_ratio must
    score that as a match — token_sort_ratio would not, since the extra
    words in the longer string lower a word-for-word comparison."""
    out = resolve_lot(listing_reserve=None,
                      listing_borrower="M/s. Sri Vaaru Traders",
                      candidates=_CANDIDATES)
    assert out["lot_key"] == "notice.jpg#3"
    assert out["method"] == RESULT_BORROWER


def test_two_lots_tied_on_reserve_price_break_on_borrower():
    tied = [
        {"lot_key": "a", "reserve": 5000000, "borrowers": ["Mr. X"]},
        {"lot_key": "b", "reserve": 5000000, "borrowers": ["M/s. Sri Vaaru Traders"]},
        {"lot_key": "c", "reserve": 9000000, "borrowers": ["Mr. Z"]},
    ]
    out = resolve_lot(listing_reserve=5000000,
                      listing_borrower="M/s. Sri Vaaru Traders",
                      candidates=tied)
    assert out["lot_key"] == "b"
    assert out["method"] == RESULT_RESERVE_BORROWER


def test_no_reserve_match_and_no_borrower_leaves_it_ambiguous():
    """Rule 4 from the design: a listing this script cannot resolve is left
    alone — no lot_key, and the caller writes no decision. Never guess."""
    out = resolve_lot(listing_reserve=999, listing_borrower=None,
                      candidates=_CANDIDATES)
    assert out["lot_key"] is None
    assert out["method"] is None


def test_a_tie_that_borrower_cannot_break_stays_unresolved():
    tied = [
        {"lot_key": "a", "reserve": 5000000, "borrowers": ["Mr. X"]},
        {"lot_key": "b", "reserve": 5000000, "borrowers": ["Mr. Y"]},
    ]
    out = resolve_lot(listing_reserve=5000000, listing_borrower="Mr. Z",
                      candidates=tied)
    assert out["lot_key"] is None


def test_two_borrower_matches_in_the_zero_reserve_case_stay_unresolved():
    """Zero reserve matches falls back to borrower over ALL candidates, not
    a narrowed subset — if that still finds two, it must not guess."""
    candidates = [
        {"lot_key": "a", "reserve": 111, "borrowers": ["M/s. Sri Vaaru Traders"]},
        {"lot_key": "b", "reserve": 222,
         "borrowers": ["M/s. Sri Vaaru Traders Extension"]},
    ]
    out = resolve_lot(listing_reserve=999,
                      listing_borrower="M/s. Sri Vaaru Traders",
                      candidates=candidates)
    assert out["lot_key"] is None


def test_no_candidates_is_unresolved_not_an_error():
    out = resolve_lot(listing_reserve=100, listing_borrower="x", candidates=[])
    assert out["lot_key"] is None


def test_a_listing_with_no_reserve_price_falls_straight_to_borrower():
    out = resolve_lot(listing_reserve=None,
                      listing_borrower="M/s. Sri Vaaru Traders",
                      candidates=_CANDIDATES)
    assert out["lot_key"] == "notice.jpg#3"
    assert out["method"] == RESULT_BORROWER


def test_lot_match_key_is_directed_not_order_independent():
    """Unlike a merge pair, a (listing, lot) match has a direction — swapping
    the two must NOT collide, unlike bank_pair_key's order-independence."""
    assert lot_match_key("796269", "notice.jpg#3") != \
        lot_match_key("notice.jpg#3", "796269")
    assert lot_match_key("796269", "notice.jpg#3") == \
        lot_match_key("796269", "notice.jpg#3")
