"""pipeline/keep_better: a re-read replaces the stored one only when better."""
from __future__ import annotations

import pytest

import pipeline.keep_better as KB


@pytest.fixture(autouse=True)
def flat_score(monkeypatch):
    """Score by entity count, so each test controls it directly."""
    monkeypatch.setattr(KB, "validate_stored",
                        lambda ents, source_text=None: {"score": len(ents)})


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
    assert KB.judge([], lot(1, "desc"), "t")[0]


def test_an_empty_new_read_never_saves():
    assert not KB.judge(lot(1, "desc"), [], "t")[0]


def test_a_gained_fact_with_nothing_lost_saves():
    save, gains, losses = KB.judge(lot(1, "desc"), lot(1, "desc", "reserve"), "t")
    assert save and "lot 1: reserve price" in gains and not losses


def test_a_lost_fact_blocks_even_with_gains():
    old = lot(1, "desc", "location") + lot(2, "desc")
    new = lot(1, "desc", "reserve") + lot(2, "desc", "reserve", "location")
    save, gains, losses = KB.judge(old, new, "t")
    assert not save and losses == ["lot 1: location"]


def test_as_good_is_not_saved():
    old = lot(1, "desc", "reserve")
    save, gains, losses = KB.judge(old, list(old), "t")
    assert not save and not gains and not losses


def test_a_lower_validator_score_blocks(monkeypatch):
    monkeypatch.setattr(KB, "validate_stored",
                        lambda ents, source_text=None:
                        {"score": 50 if len(ents) > 2 else 70})
    save, _, losses = KB.judge(lot(1, "desc"), lot(1, "desc", "reserve", "location"),
                               "t")
    assert not save and losses == ["score 70 → 50"]


def test_closer_to_the_lot_count_wins_outright():
    # an over-split notice (9 lots for 7) read correctly, even with less in it
    old = [e for i in range(1, 10) for e in lot(i, "desc", "reserve")]
    new = [e for i in range(1, 8) for e in lot(i, "desc")]
    save, gains, _ = KB.judge(old, new, "t", expected_lot_count=7)
    assert save and gains == ["lots 9 → 7 (expected 7)"]


def test_further_from_the_lot_count_loses_outright():
    old = [e for i in range(1, 8) for e in lot(i, "desc")]
    new = [e for i in range(1, 7) for e in lot(i, "desc", "reserve")]
    save, _, losses = KB.judge(old, new, "t", expected_lot_count=7)
    assert not save and losses == ["lots 7 → 6 (expected 7)"]


def test_without_a_count_fewer_lots_loses():
    old = [e for i in range(1, 4) for e in lot(i, "desc")]
    new = [e for i in range(1, 3) for e in lot(i, "desc", "reserve")]
    assert not KB.judge(old, new, "t")[0]


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
    ents, how, gains, losses = KB.best(old, new, "t")
    assert how == "merged" and not losses
    assert {"lot 1: reserve price", "lot 2: reserve price"} <= set(gains)
    assert KB._filled(ents)["1"] >= {"reserve_price", "possession_type"}


def test_best_prefers_the_new_read_when_it_is_simply_better():
    ents, how, *_ = KB.best(lot(1, "desc"), lot(1, "desc", "reserve"), "t")
    assert how == "new"


def test_best_does_not_merge_reads_with_different_lot_counts():
    old = lot(1, "desc") + [possession(1)] + lot(2, "desc")
    new = lot(1, "desc", "reserve") + lot(2, "desc") + lot(3, "desc")
    ents, how, *_ = KB.best(old, new, "t")
    assert ents is None and how == ""
