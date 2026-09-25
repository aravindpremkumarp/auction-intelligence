"""pipeline/gap_fill: short reads that fill only the key facts a lot lacks."""
from __future__ import annotations

import pipeline.gap_fill as G
from pipeline.keep_better import _filled


def _e(cls, text, md, lot="1", **attrs):
    s = md.index(text)
    return {"cls": cls, "text": text, "start": s, "end": s + len(text),
            "attrs": {"lot_index": lot, **attrs}}


MD = ("SALE NOTICE. Lot 1: land at Sy No 12/1, Village V, 1200 sq.ft. "
      "Reserve price Rs.9,50,000/-. Possession: Physical.\n")


def _stored():
    return [_e("full_description", "land at Sy No 12/1, Village V, 1200 sq.ft.", MD),
            _e("location", "Village V", MD), _e("extent", "1200 sq.ft.", MD),
            _e("property", "land", MD, property_type="land")]


def test_gaps_names_the_missing_facts_per_lot():
    assert set(G.gaps(_stored())["1"]) == {"reserve_price", "auction_date",
                                           "possession_type"}


def test_the_lean_prompt_carries_only_the_needed_classes_and_rules():
    p = G.lean_prompt(["reserve_price", "possession_type"])
    assert "- auction_terms" in p and "- property " in p
    assert "- boundary" not in p and "- secured_creditor" not in p
    assert "POSSESSION" in p and "REGISTRATION DISTRICTS" not in p
    assert len(p) < 4000                       # the full guide is ~24,000


def test_the_lean_example_holds_only_those_classes():
    ex = G.lean_example({"auction_terms"})
    assert {x.extraction_class for x in ex.extractions} == {"auction_terms"}
    assert all(x.extraction_text in ex.text for x in ex.extractions)
    assert all(x.attributes["lot_index"] == "1" for x in ex.extractions)


def test_fill_adds_what_the_read_found_and_touches_nothing_else():
    calls = []

    def read(text, keys, hint):
        calls.append(sorted(keys))
        return [_e("auction_terms", "Reserve price Rs.9,50,000/-", text,
                   reserve_price_num="950000"),
                _e("location", "Village V", text, village="WRONG")]
    stored = _stored()
    filled, report = G.fill(MD, stored, read)
    assert report == {"lots_with_gaps": 1, "reads": 1, "failed": 0}
    assert calls == [["auction_date", "possession_type", "reserve_price"]]
    assert "reserve_price" in _filled(filled)["1"]
    # the location the lot already had is untouched; the stray one is dropped
    assert [e["attrs"].get("village") for e in filled if e["cls"] == "location"] == [None]
    terms = [e for e in filled if e["cls"] == "auction_terms"][0]
    assert MD[terms["start"]:terms["end"]] == terms["text"]
    assert terms["attrs"]["gap_fill"] == "true"


def test_a_window_excerpt_maps_offsets_back_to_the_notice():
    md = "x" * 5000 + MD + "y" * 5000
    stored = [dict(e, attrs={**e["attrs"], "lot_index": "2"}) for e in [
        _e("full_description", "land at Sy No 12/1, Village V, 1200 sq.ft.", md)]]
    stored.append(_e("borrower", "x", md, lot="1"))
    text, back, hint = G.excerpt(md, stored, "2", n_lots=2)
    assert len(text) < len(md) and "land at Sy No 12/1" in hint
    i = text.index("Reserve price")
    s, _ = back(i, i + 5)
    assert md[s:s + 13] == "Reserve price"


def test_a_failed_read_keeps_the_stored_extraction():
    def read(text, keys, hint):
        raise RuntimeError("provider error")
    filled, report = G.fill(MD, _stored(), read)
    assert report["failed"] == 1
    assert _filled(filled) == _filled(_stored())
