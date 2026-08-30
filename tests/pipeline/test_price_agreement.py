"""The portal price and the notice price are two witnesses to one fact."""
import pytest

from pipeline import price_agreement as PA


# ── grading one pair ─────────────────────────────────────────────────────────

def test_identical_prices_agree():
    assert PA.compare_prices(4_500_000, 4_500_000)[0] == "agree"


def test_rounding_inside_one_percent_agrees():
    """The two sources round differently; that is not a disagreement."""
    assert PA.compare_prices(4_500_000, 4_499_000)[0] == "agree"


def test_one_percent_matches_the_matcher_s_own_tolerance():
    """If these drifted apart the two would disagree about the same pair."""
    from pipeline.apply_extractions import PRICE_TOLERANCE_PCT
    assert PA.TOLERANCE_PCT == PRICE_TOLERANCE_PCT


@pytest.mark.parametrize("portal,notice", [
    (4_500_000, 450_000),        # a dropped zero
    (4_500_000, 45_000),         # two dropped zeros
    (2_340_000, 23_400_000),     # lakh read as crore, the other way round
    (60_00_000, 6_00_00_000),
])
def test_a_clean_power_of_ten_is_a_slip_not_an_opinion(portal, notice):
    verdict, _ = PA.compare_prices(portal, notice)
    assert verdict == "magnitude_slip"
    assert PA.severity_of(verdict) == "critical"


def test_a_near_ten_x_gap_still_counts():
    """Real notices round, so 10.2x is the same defect as 10.0x."""
    assert PA.compare_prices(2_183_000, 213_000)[0] == "magnitude_slip"


def test_an_ordinary_gap_is_only_worth_a_look():
    verdict, ratio = PA.compare_prices(1_000_000, 1_400_000)
    assert verdict == "disagree"
    assert PA.severity_of(verdict) == "med"
    assert ratio == pytest.approx(1.4)


def test_a_three_x_gap_is_not_read_as_a_slip():
    assert PA.compare_prices(1_000_000, 3_000_000)[0] == "disagree"


@pytest.mark.parametrize("portal,notice", [
    (None, 4_500_000), (4_500_000, None), (None, None), ("", 4_500_000),
])
def test_a_missing_price_is_unknown_not_a_disagreement(portal, notice):
    assert PA.compare_prices(portal, notice)[0] == "unknown"


def test_zero_is_unpublished_not_a_hundred_percent_error():
    """The portal writes 0 for "price withheld".

    Grading that as a disagreement would bury the 40 real ones under noise.
    """
    assert PA.compare_prices(0, 4_500_000)[0] == "unknown"


def test_agreement_is_not_a_finding():
    assert PA.severity_of("agree") is None
    assert PA.severity_of("unknown") is None


# ── one matched pair ─────────────────────────────────────────────────────────

def _pair(portal, notice, reason="exact"):
    return ({"aid": "1", "price": portal},
            {"lot_index": "1", "reserve": notice}, reason)


def test_a_matching_pair_produces_nothing():
    assert PA.check_match(*_pair(4_500_000, 4_500_000)) is None


def test_a_slip_is_reported_with_both_numbers():
    f = PA.check_match(*_pair(4_500_000, 45_000, "borrower"))
    assert f["verdict"] == "magnitude_slip"
    assert f["severity"] == "critical"
    assert (f["portal_price"], f["notice_price"]) == (4_500_000, 45_000)
    assert f["matched_by"] == "borrower"


def test_a_fallback_match_is_itself_the_evidence():
    """The cascade tries price FIRST.

    So a pair that matched on `borrower` has already proved the prices did
    not line up — it is reported even when a price is missing, because the
    fallback is what carries the information.
    """
    f = PA.check_match(*_pair(None, None, "borrower"))
    assert f is not None and f["verdict"] == "disagree"


def test_a_single_lot_notice_with_no_price_is_not_accused():
    """`single` means "there is one lot", not "the price failed"."""
    assert PA.check_match(*_pair(None, None, "single")) is None


def test_a_price_match_with_a_missing_price_is_not_a_finding():
    assert PA.check_match(*_pair(4_500_000, None, "exact")) is None


def test_price_reasons_are_the_matcher_s_price_keys():
    assert PA.PRICE_REASONS == {"exact", "tolerance"}


# ── a whole notice ───────────────────────────────────────────────────────────

def test_findings_are_worst_first():
    rows = PA.check_document([
        _pair(1_000_000, 1_400_000, "emd"),
        ({"aid": "2", "price": 4_500_000},
         {"lot_index": "2", "reserve": 45_000}, "emd"),
    ])
    assert [f["severity"] for f in rows] == ["critical", "med"]


def test_a_clean_notice_yields_nothing():
    assert PA.check_document([_pair(4_500_000, 4_500_000)]) == []


def test_check_document_survives_an_empty_notice():
    assert PA.check_document([]) == []
