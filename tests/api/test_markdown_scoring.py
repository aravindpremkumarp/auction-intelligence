"""Unit tests for the blended markdown-quality score.

Covers the matcher building blocks in api/review/markdown_match.py and the
document-level blend in pipeline/score_markdown.py. All pure functions — no
Neo4j required.
"""
from __future__ import annotations

from api.review.markdown_match import (
    borrower_in_markdown,
    description_coverage,
    price_in_markdown,
)
from pipeline.score_markdown import (
    W_BORROWER,
    W_COVERAGE,
    W_PRICE,
    _score_one,
    _score_property,
)


# A real-world pair (from the review UI): the scraped website description and
# the OCR markdown of the same notice, with the usual OCR / scraper noise
# ("atM.N." vs "at M.N.", "An-chaneyar" vs "Anchaneyar", trailing field bleed).
WEBSITE = (
    "Property description: All that Piece and Parcel of the land and building in "
    "Plot No.2 situated atM.N.Ethiraj Mudaliyar Nagar, Sholingur Town, Wallajah "
    "Tk, Ranipet Dt., measuring 1160 Sq.Ft of land comprised in Old Survey "
    "No.464/2, as per Sub- Division New Survey No.464/2A and as per Patta New "
    "Survey No.464/5 within the Sub-Registration District of Sholingur within "
    "the boundaries hereunder South by: 23 Ft of Sri Yoga An-chaneyar Street"
)
MARKDOWN = (
    "| | 09/01/2026 | |\n"
    "Property description: All that Piece and Parcel of the land and building in "
    "Plot No.2 situated at M.N.Ethiraj Mudaliyar Nagar, Sholingur Town, Wallajah "
    "Tk, Ranipet Dt., measuring 1160 Sq.Ft of land comprised in Old Survey "
    "No.464/2, as per Sub- Division New Survey No.464/2A and as per Patta New "
    "Survey No.464/5 within the Sub-Registration District of Sholingur within "
    "the boundaries hereunder South by: 23 Ft of Sri Yoga Anchaneyar Street\n"
    "| 2. | 1. GANDHI K LOGANATHAN | Reserve Price Rs. 71,23,450 |"
)


# ── description_coverage ────────────────────────────────────────────────────

def test_coverage_near_verbatim_match_scores_high():
    score, span = description_coverage(WEBSITE, MARKDOWN)
    assert score >= 90.0
    assert span is not None and span[1] > span[0]


def test_coverage_unrelated_text_scores_low():
    score, _ = description_coverage(WEBSITE, "completely unrelated tender notice text")
    assert score < 50.0


def test_coverage_empty_inputs_are_zero():
    assert description_coverage("", MARKDOWN) == (0.0, None)
    assert description_coverage(WEBSITE, "") == (0.0, None)
    assert description_coverage(None, None) == (0.0, None)


def test_coverage_ignores_hyphenation_and_label_noise():
    # Same content, only label + hyphen-break differences → still near-perfect.
    a = "Property description: Sub- Division New Survey within An-chaneyar Street"
    b = "Sub-Division New Survey within Anchaneyar Street"
    score, _ = description_coverage(a, b)
    assert score >= 95.0


# ── price / borrower presence ───────────────────────────────────────────────

def test_price_in_markdown_indian_and_international():
    assert price_in_markdown(7123450, "Reserve Price Rs. 71,23,450") is True
    assert price_in_markdown(7123450, "Reserve Price Rs. 7,123,450") is True
    assert price_in_markdown(7123450, "no price here") is False
    assert price_in_markdown(None, "Rs. 71,23,450") is False


def test_borrower_in_markdown_matches_distinguishing_token():
    assert borrower_in_markdown(["Mr K Loganathan"], "borrower: LOGANATHAN") is True
    # honorific-only / too-short tokens don't false-positive
    assert borrower_in_markdown(["Mr A"], "some text") is False
    assert borrower_in_markdown([], "any text") is False


# ── _score_property / _score_one blend ──────────────────────────────────────

def test_weights_sum_to_one():
    assert abs((W_COVERAGE + W_PRICE + W_BORROWER) - 1.0) < 1e-9


def test_property_without_website_description_is_unscored():
    assert _score_property(MARKDOWN, {"reserve_price": 7123450}) is None
    assert _score_property(MARKDOWN, {"website_description": "   "}) is None


def test_property_blend_combines_coverage_price_borrower():
    prop = {
        "website_description": WEBSITE,
        "reserve_price": 7123450,
        "borrowers": ["Mr K Loganathan"],
    }
    coverage, _ = description_coverage(WEBSITE, MARKDOWN)
    expected = W_COVERAGE * coverage + W_PRICE * 100.0 + W_BORROWER * 100.0
    assert abs(_score_property(MARKDOWN, prop) - expected) < 1e-6


def test_missing_price_and_borrower_only_earns_coverage_weight():
    prop = {"website_description": WEBSITE}  # no price, no borrower in markdown
    coverage, _ = description_coverage(WEBSITE, MARKDOWN)
    assert abs(_score_property(MARKDOWN, prop) - W_COVERAGE * coverage) < 1e-6


def test_document_score_is_mean_over_scorable_properties():
    good = {"website_description": WEBSITE, "reserve_price": 7123450,
            "borrowers": ["Mr K Loganathan"]}
    no_desc = {"reserve_price": 999}  # ignored — no website_description
    one = _score_property(MARKDOWN, good)
    assert abs(_score_one(MARKDOWN, [good, no_desc]) - round(one, 1)) < 0.05


def test_document_unscored_when_no_property_has_description():
    assert _score_one(MARKDOWN, [{"reserve_price": 7123450}]) is None
    assert _score_one(MARKDOWN, []) is None
    assert _score_one("", [{"website_description": WEBSITE}]) is None
