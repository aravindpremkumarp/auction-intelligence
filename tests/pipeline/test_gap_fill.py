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
    assert report == {"lots_with_gaps": 1, "reads": 1, "failed": 0,
                      "read_lots": ["1"]}
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


def test_possession_stated_in_the_header_is_every_lots():
    md = ("The physical possession of the properties has been taken. "
          "Lot 1: land at Sy No 1. Lot 2: land at Sy No 2.")

    def e(cls, text, lot, **a):
        s = md.index(text)
        return {"cls": cls, "text": text, "start": s, "end": s + len(text),
                "attrs": {"lot_index": lot, **a}}
    ents = [e("property", "physical possession", "1", possession_type="physical"),
            e("full_description", "land at Sy No 1", "1"),
            e("full_description", "land at Sy No 2", "2"),
            e("property", "land at Sy No 2", "2", property_type="land")]
    out = G.inherit_shared(ents)
    assert "possession_type" in _filled(out)["2"]
    lot2 = [x for x in out if x["cls"] == "property" and x["attrs"]["lot_index"] == "2"]
    assert len(lot2) == 1 and lot2[0]["attrs"]["possession_type"] == "physical"
    # a possession stated inside a lot's own text is not spread
    ents[0] = e("property", "land at Sy No 1", "1", possession_type="physical")
    assert "possession_type" not in _filled(G.inherit_shared(ents))["2"]


def test_a_header_naming_one_possession_type_fills_every_lot_without_a_read():
    md = ("Whereas the physical possession of the properties has been taken. "
          "Lot 1: land at Sy No 1. Lot 2: land at Sy No 2.")

    def e(cls, text, lot, **a):
        s = md.index(text)
        return {"cls": cls, "text": text, "start": s, "end": s + len(text),
                "attrs": {"lot_index": lot, **a}}
    ents = [e("full_description", "land at Sy No 1", "1"),
            e("full_description", "land at Sy No 2", "2")]
    out = G.inherit_shared(ents, md)
    assert {"1", "2"} <= {k for k, v in _filled(out).items() if "possession_type" in v}
    # a header that lists types without choosing states none
    md2 = md.replace("physical possession", "symbolic / constructive possession")
    ents2 = [dict(x, start=md2.index(x["text"]), end=md2.index(x["text"]) + len(x["text"]))
             for x in ents]
    assert not any("possession_type" in v for v in _filled(G.inherit_shared(ents2, md2)).values())


# ── the conditional possession boilerplate (Canara Bank) ────────────────────

_CANARA_TAIL = (
    "For the properties which are in symbolic possession of the bank, the "
    "Auction purchaser has to comply with the following terms and conditions "
    "in addition to the standard terms and condition of the sale.\n\n"
    "1. The bidder is purchasing the property in Symbolic Possession at his "
    "own risk and responsibility. 2. Bank will not be responsible or duly "
    "bound for handing over of physical possession. 3. Successful Auction "
    "Purchaser will not be entitled to claim any interest. 5. Subsequent to "
    "sale if successful bidder fails to submit Declaration cum Undertaking, "
    "the bid EMD amount will be forfeited.\n\nPortal of E-Auction: https://baanknet.com")

_CANARA = (
    "the Symbolic / Constructive / Physical Possession of which has been taken "
    "by the Authorized Officer. DETAILS OF PROPERTY: Property No.1 land at "
    "Sy No 176/2A, Kavanoor Village. RESERVE PRICE 35,25,000/\n\n"
    "Property No.2 land and building at Door no.419, Kavanoor Village. "
    "RESERVE PRICE 44,00,000/\n\n" + _CANARA_TAIL)


def test_the_boilerplate_is_blanked_without_moving_any_offset():
    out = G.mask_possession_boilerplate(_CANARA)
    assert len(out) == len(_CANARA)
    assert [i for i, c in enumerate(_CANARA) if c == "\n"] == \
           [i for i, c in enumerate(out) if c == "\n"]
    tail = out[_CANARA.index("For the properties"):]
    assert "symbolic" not in tail.lower() and "physical" not in tail.lower()
    # what follows the block survives, and so does the rest of the notice
    assert "Portal of E-Auction: https://baanknet.com" in out
    assert out.startswith("the Symbolic / Constructive / Physical Possession")
    assert G.mask_possession_boilerplate("no block here") == "no block here"


def test_a_block_without_its_closing_words_loses_only_its_type_words():
    cut = _CANARA_TAIL.split("5. Subsequent")[0] + "Lot 3: vacant land, Sy No 9."
    out = G.mask_possession_boilerplate(cut)
    assert len(out) == len(cut)
    assert "symbolic" not in out.lower() and "physical" not in out.lower()
    assert "Lot 3: vacant land, Sy No 9." in out and "bidder is purchasing" in out


def test_boilerplate_and_an_unchosen_menu_state_no_possession():
    assert G.stated_possession_kinds(_CANARA) == set()
    # a notice that commits to one type still states it, boilerplate or not
    md = _CANARA.replace("Symbolic / Constructive / Physical Possession",
                         "Physical Possession")
    assert G.stated_possession_kinds(md) == {"physical"}


def test_a_lot_whose_only_possession_is_the_boilerplate_is_marked_without_a_read():
    from pipeline.absence import RULE_NO_CLUE
    # The menu is still read (the guide tells the model to emit nothing for
    # it); here the boilerplate is the notice's only possession wording.
    md = _CANARA.replace("the Symbolic / Constructive / Physical Possession",
                         "the possession")
    ents = [_e("full_description", "land at Sy No 176/2A, Kavanoor Village", md, "1"),
            _e("full_description", "land and building at Door no.419, Kavanoor Village",
               md, "2")]
    todo, marks = G.plan(md, ents, expected_lot_count=2)
    # both lots get the same answer: nothing to read, not in the notice
    assert marks.get(("1", "possession_type")) == RULE_NO_CLUE
    assert marks.get(("2", "possession_type")) == RULE_NO_CLUE
    assert all("possession_type" not in ks for ks in todo.values())


def test_a_possession_taken_from_the_boilerplate_is_cleared_and_recorded():
    ents = [_e("property", "land at Sy No 176/2A", _CANARA, "1", property_type="land"),
            dict(_e("property", "land and building at Door no.419", _CANARA, "2",
                    property_type="house", possession_type="symbolic"), id="p2")]
    kept, cleared, absent = G.unsupported_possession(ents, _CANARA)
    assert cleared == [{"id": "p2", "lot": "2", "value": "symbolic", "cls": "property"}]
    assert all("possession_type" not in (e["attrs"]) for e in kept)
    assert kept[1]["attrs"]["property_type"] == "house"       # nothing else touched
    assert absent == {"2"}
    # a value the notice does state is kept
    md = _CANARA.replace("Symbolic / Constructive / Physical Possession",
                         "Symbolic Possession")
    ents2 = [dict(x, start=md.index(x["text"]), end=md.index(x["text"]) + len(x["text"]))
             for x in ents]
    kept2, cleared2, absent2 = G.unsupported_possession(ents2, md)
    assert cleared2 == [] and kept2[1]["attrs"]["possession_type"] == "symbolic"
    # a notice stating another type: the wrong value goes, but the lot is left
    # missing for a read, not declared absent
    md3 = _CANARA.replace("Symbolic / Constructive / Physical Possession",
                          "Physical Possession")
    ents3 = [dict(x, start=md3.index(x["text"]), end=md3.index(x["text"]) + len(x["text"]))
             for x in ents]
    _, cleared3, absent3 = G.unsupported_possession(ents3, md3)
    assert [c["value"] for c in cleared3] == ["symbolic"] and absent3 == set()
