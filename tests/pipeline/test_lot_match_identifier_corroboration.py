"""A unit identifier both sides quote, corroborating the lot's own claim.

The case: ten flats in one complex, two reserve prices between them alternating
with the unit letter. An exact price match narrows to five and never to one —
and where the portal has two siblings' prices swapped, it narrows to the wrong
five, eliminating the listing's own lot before any other tier sees it. The flat
number is exact and unique; the price is not.

`_claim_contradicted` then vetoes on that same wrong price, so a correctly
matched listing is logged as a conflict against a lot it demonstrably is not.
"""
from __future__ import annotations

from pipeline.apply_extractions import match_lots_to_listings


def _lot(i, reserve, tokens, claim=None):
    return {"lot_index": str(i), "reserve": reserve, "emd": None,
            "portal_aid": claim, "id_tokens": set(tokens),
            "borrower_tokens": set(), "fields": {}, "description": ""}


#: Flats 2C and 2D, with the portal's reserves swapped between them.
_2C = _lot(7, 2696000, {"2c"}, claim="749856")
_2D = _lot(8, 2698000, {"2d"}, claim="749855")
#: Two more lots sharing 2C's price, so price alone cannot decide.
_OTHER = [_lot(2, 2696000, {"1h"}), _lot(4, 2696000, {"2h"})]


def _lots():
    return {l["lot_index"]: l for l in [*_OTHER, _2C, _2D]}


def _listing(aid, price, unit):
    return {"aid": aid, "price": price, "emd": None, "borrowers": [],
            "id_text": f"Flat No. {unit}, Block 2, Second Floor"}


def test_the_identifier_rescues_a_claim_the_swapped_price_would_veto():
    listing = _listing("749855", 2696000, "2D")   # price says 2C's lot
    matches, unmatched = match_lots_to_listings(_lots(), [listing])
    assert not unmatched, unmatched
    assert matches[0][1]["lot_index"] == "8"
    assert matches[0][2] == "identifier"


def test_without_the_unit_in_the_portal_text_it_stays_a_conflict():
    """The rescue is the identifier, not a general softening of the price
    check: strip the flat number and the conflict must come back."""
    listing = dict(_listing("749855", 2696000, "2D"), id_text="Block 2, Second Floor")
    _, unmatched = match_lots_to_listings(_lots(), [listing])
    assert unmatched and unmatched[0][1] == "portal_aid_conflict"


def test_an_identifier_naming_a_different_lot_than_the_claim_is_refused():
    """Corroboration only. Two independent signals disagreeing is the case this
    matcher refuses to guess at, and an identifier is not exempt."""
    listing = dict(_listing("749855", 2696000, "2C"))  # unit says 2C, claim says 2D
    _, unmatched = match_lots_to_listings(_lots(), [listing])
    assert unmatched and unmatched[0][1] == "portal_aid_conflict"


def test_a_price_that_already_agrees_is_untouched():
    """The ordinary path must keep its own reason — the rescue only fires where
    the claim was excluded."""
    listing = _listing("749856", 2696000, "2C")
    matches, unmatched = match_lots_to_listings(_lots(), [listing])
    assert not unmatched
    assert matches[0][1]["lot_index"] == "7"


def test_a_shared_token_cannot_identify_anything():
    """Sibling flats quote the same land. Only what the lots do NOT say in
    common can separate them."""
    lots = {l["lot_index"]: l for l in [
        _lot(1, 2696000, {"45/2", "2c"}, claim="A"),
        _lot(2, 2698000, {"45/2", "2d"}, claim="B"),
    ]}
    listing = {"aid": "B", "price": 2696000, "emd": None, "borrowers": [],
               "id_text": "Survey 45/2"}
    _, unmatched = match_lots_to_listings(lots, [listing])
    assert unmatched and unmatched[0][1] == "portal_aid_conflict"
