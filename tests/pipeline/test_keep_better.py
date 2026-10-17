"""pipeline/keep_better: a re-read replaces the stored one only when better."""
from __future__ import annotations

import pytest

import pipeline.keep_better as KB


@pytest.fixture(autouse=True)
def flat_score(monkeypatch):
    """Score by entity count, so each test controls it directly."""
    monkeypatch.setattr(KB, "validate_stored",
                        lambda ents, source_text=None: {"score": len(ents)})


#: The notice text the reads point into. It prints the stand-in reserve price,
#: so a lot's price counts (keep_better.price_doubts).
NOTICE = "Reserve Price Rs.1,00,000/-"


def lot(i, *facts):
    """Entities for lot ``i`` carrying the named key facts."""
    out = []
    for f in facts:
        if f == "reserve":
            out.append({"cls": "auction_terms", "text": "Rs.1", "attrs": {
                "lot_index": str(i), "reserve_price_num": "100000"}})
        elif f == "location":
            out.append({"cls": "location", "text": "Village V",
                        "attrs": {"lot_index": str(i)}})
        elif f == "desc":
            out.append({"cls": "full_description", "text": "All that piece",
                        "attrs": {"lot_index": str(i)}})
    return out


def test_no_stored_read_always_saves():
    assert KB.judge([], lot(1, "desc"), NOTICE)[0]


def test_an_empty_new_read_never_saves():
    assert not KB.judge(lot(1, "desc"), [], NOTICE)[0]


def test_a_gained_fact_with_nothing_lost_saves():
    save, gains, losses = KB.judge(lot(1, "desc"), lot(1, "desc", "reserve"), NOTICE)
    assert save and "lot 1: reserve price" in gains and not losses


def test_a_lost_fact_blocks_even_with_gains():
    old = lot(1, "desc", "location") + lot(2, "desc")
    new = lot(1, "desc", "reserve") + lot(2, "desc", "reserve", "location")
    save, gains, losses = KB.judge(old, new, NOTICE)
    assert not save and losses == ["lot 1: location"]


def test_as_good_is_not_saved():
    old = lot(1, "desc", "reserve")
    save, gains, losses = KB.judge(old, list(old), NOTICE)
    assert not save and not gains and not losses


def test_a_lower_validator_score_blocks(monkeypatch):
    monkeypatch.setattr(KB, "validate_stored",
                        lambda ents, source_text=None:
                        {"score": 50 if len(ents) > 2 else 70})
    save, _, losses = KB.judge(lot(1, "desc"), lot(1, "desc", "reserve", "location"),
                               NOTICE)
    assert not save and losses == ["score 70 → 50"]


def test_closer_to_the_lot_count_wins_outright():
    # an over-split notice (9 lots for 7) read correctly, even with less in it
    old = [e for i in range(1, 10) for e in lot(i, "desc", "reserve")]
    new = [e for i in range(1, 8) for e in lot(i, "desc")]
    save, gains, _ = KB.judge(old, new, NOTICE, expected_lot_count=7)
    assert save and gains == ["lots 9 → 7 (expected 7)"]


def test_further_from_the_lot_count_loses_outright():
    old = [e for i in range(1, 8) for e in lot(i, "desc")]
    new = [e for i in range(1, 7) for e in lot(i, "desc", "reserve")]
    save, _, losses = KB.judge(old, new, NOTICE, expected_lot_count=7)
    assert not save and losses == ["lots 7 → 6 (expected 7)"]


def test_without_a_count_fewer_lots_loses():
    old = [e for i in range(1, 4) for e in lot(i, "desc")]
    new = [e for i in range(1, 3) for e in lot(i, "desc", "reserve")]
    assert not KB.judge(old, new, NOTICE)[0]


# ── step 2: merge lot by lot when each read has what the other lacks ─────────

def possession(i, value="Physical"):
    return {"cls": "property", "text": "house", "attrs": {
        "lot_index": str(i), "possession_type": value}}


def test_merge_fills_the_missing_fact_and_keeps_the_rest():
    old = lot(1, "desc") + [possession(1)]
    new = lot(1, "desc", "reserve")
    merged = KB.merge(old, new)
    classes = [e["cls"] for e in merged]
    assert classes.count("auction_terms") == 1 and classes.count("property") == 1
    assert KB._filled(merged)["1"] >= {"reserve_price", "possession_type",
                                       "full_description"}
    assert merged[-1]["attrs"]["merged"] == "true"
    assert old == lot(1, "desc") + [possession(1)]        # base untouched


def test_merge_sets_an_attribute_on_the_bases_own_entity():
    old = lot(1, "desc") + [{"cls": "property", "text": "house",
                             "attrs": {"lot_index": "1"}}]
    new = lot(1, "desc") + [possession(1, "Symbolic")]
    merged = KB.merge(old, new)
    props = [e for e in merged if e["cls"] == "property"]
    assert len(props) == 1
    assert props[0]["attrs"]["possession_type"] == "Symbolic"
    assert props[0]["attrs"]["merged_attrs"] == "possession_type"


def test_merge_never_overwrites_a_fact_the_base_has():
    old = lot(1, "desc") + [possession(1, "Physical")]
    new = lot(1, "desc") + [possession(1, "Symbolic")]
    merged = KB.merge(old, new)
    assert [e["attrs"]["possession_type"] for e in merged
            if e["cls"] == "property"] == ["Physical"]


def test_best_takes_the_merge_when_each_read_lacks_something():
    old = lot(1, "desc") + [possession(1)] + lot(2, "desc")
    new = lot(1, "desc", "reserve") + lot(2, "desc", "reserve")
    ents, how, gains, losses = KB.best(old, new, NOTICE)
    assert how == "merged" and not losses
    assert {"lot 1: reserve price", "lot 2: reserve price"} <= set(gains)
    assert KB._filled(ents)["1"] >= {"reserve_price", "possession_type"}


def test_best_prefers_the_new_read_when_it_is_simply_better():
    ents, how, *_ = KB.best(lot(1, "desc"), lot(1, "desc", "reserve"), NOTICE)
    assert how == "new"


def test_best_does_not_merge_reads_with_different_lot_counts():
    old = lot(1, "desc") + [possession(1)] + lot(2, "desc")
    new = lot(1, "desc", "reserve") + lot(2, "desc") + lot(3, "desc")
    ents, how, *_ = KB.best(old, new, NOTICE)
    assert ents is None and how == ""


def test_a_location_part_counts_as_a_fact():
    def loc(**parts):
        return [{"cls": "location", "text": "Adhanur Village",
                 "attrs": {"lot_index": "1", **parts}}]
    save, gains, losses = KB.judge(loc(), loc(village="Adhanur"), "")
    assert save and gains == ["lot 1: village"] and not losses
    save, gains, losses = KB.judge(loc(village="Adhanur"), loc(taluk="Kundrathur"), "")
    assert not save and losses == ["lot 1: village"]
    # best merges the two, keeping both parts
    merged, how, _, _ = KB.best(loc(village="Adhanur"),
                                [{"cls": "location", "text": "Kundrathur Taluk",
                                  "attrs": {"lot_index": "1", "taluk": "Kundrathur"}}], "")
    assert how == "merged"
    assert {k for e in merged for k in e["attrs"]} >= {"village", "taluk"}


# ── a re-read's new price must be that lot's own ─────────────────────────────
# Built on three real re-reads that replaced a good stored read: lot 3's price
# line copied onto lot 4 (twice), and a figure printed nowhere in the notice.

_PRICED = ("Lot 1. House at Village A.\nRESERVE PRICE Rs.17,80,000/- EMD Rs.1,78,000/-\n"
           "Lot 2. Plot at Village B.\nRESERVE PRICE Rs.10,40,000/- EMD Rs.1,04,000/-\n"
           "Lot 3. Plot at Village C. (no price printed for this lot)\n")


def _priced(lot_index, value, needle):
    """An auction_terms entity whose span is ``needle`` in _PRICED."""
    s = _PRICED.index(needle)
    return {"cls": "auction_terms", "text": needle, "start": s, "end": s + len(needle),
            "attrs": {"lot_index": lot_index, "reserve_price_num": str(value)}}


def _lots(*lots):
    return [e for li in lots for e in lot(li, "desc")]


def test_a_price_copied_from_a_sibling_lots_line_blocks_the_read():
    line2 = "RESERVE PRICE Rs.10,40,000/-"
    old = _lots(1, 2, 3) + [_priced("1", 1780000, "RESERVE PRICE Rs.17,80,000/-"),
                            _priced("2", 1040000, line2)]
    new = old + [_priced("3", 1040000, line2)]
    save, gains, losses = KB.judge(old, new, _PRICED)
    assert not save and not gains
    assert losses == ["lot 3: reserve price 1040000 — price line shared by lots 2, 3"]


def test_a_price_printed_nowhere_in_the_notice_blocks_the_read():
    old = _lots(1, 2, 3)
    made_up = {"cls": "auction_terms", "text": "RESERVE PRICE Rs.37,70,000/-",
               "start": 0, "end": 10, "attrs": {"lot_index": "3", "reserve_price_num": "3770000"}}
    save, _, losses = KB.judge(old, old + [made_up], _PRICED)
    assert not save
    assert losses == ["lot 3: reserve price 3770000 — not printed anywhere in the notice"]


def test_a_line_that_names_both_plots_may_price_both():
    text = "Plot No.16 & 17 - Rs. 13,50,000/- each\n"
    span = {"start": 0, "end": len(text) - 1}
    old = _lots(1, 2)
    new = old + [{"cls": "auction_terms", "text": text.strip(), **span,
                  "attrs": {"lot_index": li, "reserve_price_num": "1350000"}} for li in ("1", "2")]
    save, gains, losses = KB.judge(old, new, text)
    assert save and not losses
    assert {"lot 1: reserve price", "lot 2: reserve price"} <= set(gains)


def test_a_doubtful_price_the_stored_read_already_held_is_not_a_new_loss():
    """The check stops a re-read from ADDING a wrong price. One the stored read
    already carries earns neither read credit, so it cannot block a re-read
    that gains something else."""
    line2 = "RESERVE PRICE Rs.10,40,000/-"
    shared = [_priced("2", 1040000, line2), _priced("3", 1040000, line2)]
    old = _lots(1, 2, 3) + shared
    new = old + lot(1, "location")
    save, gains, losses = KB.judge(old, new, _PRICED)
    assert save and "lot 1: location" in gains and not losses
    assert not [g for g in gains if "reserve price" in g]


def test_ocr_separator_slips_still_count_as_printed():
    assert {194000, 8900000, 3150000, 18200000} <= KB._amounts(
        "Rs.1.94,000/- ₹ 89,00.000/- Rs. 31,50 Lakh Rs.<br/>1,82,00,<br/>000/-")


def test_merge_does_not_carry_a_price_standing_on_a_siblings_line():
    line2 = "RESERVE PRICE Rs.10,40,000/-"
    base = _lots(2, 3) + [_priced("2", 1040000, line2)]
    donor = _lots(2, 3) + [_priced("2", 1040000, line2), _priced("3", 1040000, line2)]
    merged = KB.merge(base, donor, _PRICED)
    assert not [e for e in merged if e["cls"] == "auction_terms" and e["attrs"]["lot_index"] == "3"]
    # without the notice text there is nothing to check against, as before
    assert [e for e in KB.merge(base, donor) if e["cls"] == "auction_terms"
            and e["attrs"]["lot_index"] == "3"]


# ── a changed notice text is no free pass ────────────────────────────────────

def test_a_thinner_read_of_a_changed_text_is_refused():
    old = [e for i in range(1, 6) for e in lot(i, "desc", "reserve", "location")]
    new = [e for i in range(1, 6) for e in lot(i, "desc")]
    assert KB.stale_losses(old, new) == ["key facts 15 → 5 (3.0 → 1.0 per lot)"]


def test_a_changed_text_read_further_from_the_lot_count_is_refused():
    old = [e for i in range(1, 8) for e in lot(i, "desc")]
    new = [e for i in range(1, 6) for e in lot(i, "desc", "reserve")]
    assert KB.stale_losses(old, new, 7) == ["lots 7 → 5 (expected 7)"]


def test_a_changed_text_read_closer_to_the_lot_count_still_wins():
    old = [e for i in range(1, 10) for e in lot(i, "desc", "reserve")]
    new = [e for i in range(1, 8) for e in lot(i, "desc")]
    assert KB.stale_losses(old, new, 7) == []


def test_folding_duplicate_lots_is_not_thinner():
    """Fewer facts in all, the same per lot: a re-split, not a worse read."""
    old = [e for i in range(1, 9) for e in lot(i, "desc", "reserve")]
    new = [e for i in range(1, 7) for e in lot(i, "desc", "reserve")]
    assert KB.stale_losses(old, new) == []
