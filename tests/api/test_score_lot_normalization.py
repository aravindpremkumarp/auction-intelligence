"""Guard: extraction_score must measure extraction QUALITY, not lot count.

Every validator check that can span lots emits ONE flag naming the affected
lots, never one flag per lot. Emitting per-lot made a systematic defect
multiply: with high=20, the same quirk recurring across 5 lots floored the
document at 0 on its own, so a large-but-good notice scored identically to a
small-but-broken one and review triage (and the eval loop) lost its signal.

Pure test — no langextract, no API key, no DB.
"""
from __future__ import annotations

from types import SimpleNamespace

from pipeline.validators import validate

_SPAN = SimpleNamespace(start_pos=0, end_pos=1)


def _flat_lot(li: str, *, uds_parent: float, own_area: float) -> list:
    """One flat lot that trips uds_parent_as_own_area (parent extent echoed as
    the flat's own area), plus the entities needed to keep unrelated checks
    quiet so the test isolates the multiplication behaviour."""
    return [
        SimpleNamespace(extraction_class="property", extraction_text="Flat",
                        attributes={"lot_index": li, "property_type": "flat",
                                    "possession_type": "physical"},
                        char_interval=_SPAN),
        SimpleNamespace(extraction_class="extent", extraction_text="760 sq.ft",
                        attributes={"lot_index": li, "undivided_share": "yes",
                                    "uds_parent_extent": uds_parent,
                                    "total_area": own_area},
                        char_interval=_SPAN),
        SimpleNamespace(extraction_class="borrower", extraction_text="B",
                        attributes={"lot_index": li}, char_interval=_SPAN),
        SimpleNamespace(extraction_class="auction_terms", extraction_text="RP",
                        attributes={"lot_index": li, "reserve_price_num": 5_000_000,
                                    "emd_num": 500_000},
                        char_interval=_SPAN),
    ]


def _notice(n_lots: int) -> list:
    ents = [SimpleNamespace(
        extraction_class="secured_creditor", extraction_text="Bank",
        attributes={"legal_basis": "SARFAESI", "lot_index": "1"},
        char_interval=_SPAN)]
    for i in range(1, n_lots + 1):
        ents += _flat_lot(str(i), uds_parent=2257.0, own_area=2257.0)
    return ents


def _codes(report) -> list[str]:
    return [i["code"] for i in report["issues"]]


def test_recurring_per_lot_defect_flags_once_not_per_lot():
    six = validate(_notice(6))
    assert _codes(six).count("uds_parent_as_own_area") == 1, (
        "a defect recurring across 6 lots must cost one penalty, not six")


def test_score_does_not_degrade_with_lot_count_for_the_same_defect():
    """Same defect rate (every lot affected) across 1, 3 and 6 lots — the score
    must not fall just because the notice is bigger."""
    scores = {n: validate(_notice(n))["score"] for n in (1, 3, 6)}
    assert len(set(scores.values())) == 1, (
        f"score varies with lot count for an identical defect: {scores}")


def test_affected_lots_still_named_in_the_message():
    """Aggregating must not lose which lots are affected — reviewers need it."""
    msg = next(i["msg"] for i in validate(_notice(3))["issues"]
               if i["code"] == "uds_parent_as_own_area")
    for li in ("1", "2", "3"):
        assert li in msg


def test_genuinely_broken_notice_still_bottoms_out():
    """Normalization must not rescue a real failure: a notice missing creditor,
    borrower, location, reserve price and extent has five DISTINCT defects and
    must still score near zero — and well below the large-but-good notice."""
    lone = [SimpleNamespace(extraction_class="extras", extraction_text="x",
                            attributes={"lot_index": "1"}, char_interval=_SPAN)]
    report = validate(lone)
    assert report["score"] <= 10
    assert len(set(_codes(report))) >= 5
    # The whole point: a big notice with ONE systematic defect now outranks a
    # small notice that failed outright. Before the fix both sat at 0.
    assert validate(_notice(6))["score"] > report["score"]


def test_reserve_and_emd_checks_also_aggregate():
    """The other two former per-lot loops collapse to one flag each."""
    ents = [SimpleNamespace(
        extraction_class="secured_creditor", extraction_text="Bank",
        attributes={"legal_basis": "SARFAESI", "lot_index": "1"},
        char_interval=_SPAN)]
    for i in range(1, 5):
        li = str(i)
        ents += [
            SimpleNamespace(extraction_class="property", extraction_text="Land",
                            attributes={"lot_index": li, "property_type": "land",
                                        "possession_type": "physical"},
                            char_interval=_SPAN),
            SimpleNamespace(extraction_class="borrower", extraction_text="B",
                            attributes={"lot_index": li}, char_interval=_SPAN),
            SimpleNamespace(extraction_class="extent", extraction_text="1 acre",
                            attributes={"lot_index": li, "total_area": 43560},
                            char_interval=_SPAN),
            # reserve below _RESERVE_MIN, and emd/reserve far off ~0.10
            SimpleNamespace(extraction_class="auction_terms", extraction_text="RP",
                            attributes={"lot_index": li, "reserve_price_num": 500,
                                        "emd_num": 450},
                            char_interval=_SPAN),
        ]
    codes = _codes(validate(ents))
    assert codes.count("reserve_out_of_range") == 1
    assert codes.count("emd_ratio_off") == 1
