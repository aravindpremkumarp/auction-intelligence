from __future__ import annotations

from pipeline.promote_extractions import lot_provenance


def test_lot_provenance_keys_each_value_to_its_receipt():
    ents = [
        {"id": "e1", "cls": "auction_terms", "text": "Rs.1", "start": 10, "end": 14,
         "attrs": {"lot_index": "1", "reserve_price_num": "100000", "emd_num": "10000",
                   "evidence": "VERIFIED", "page": 1, "source": "table:r1:c3", "method": "structured_reader",
                   "inherited": "auction_start_dt", "auction_start_dt": "2026-10-15"}},
        {"id": "e2", "cls": "location", "text": "x", "start": 20, "end": 21,
         "attrs": {"lot_index": "1", "village": "Padur", "evidence": "EXPLICIT", "page": 1, "source": "prose"}},
        {"id": "e3", "cls": "full_description", "text": "x", "start": 30, "end": 90,
         "attrs": {"lot_index": "2", "evidence": "EXPLICIT", "page": 2}},
        {"id": "old", "cls": "borrower", "text": "x", "start": 0, "end": 1, "attrs": {"lot_index": "2"}},
    ]
    prov = lot_provenance(ents)
    assert prov["1"]["reserve_price_num"]["entity_id"] == "e1"
    assert prov["1"]["reserve_price_num"]["source"] == "table:r1:c3"
    assert prov["1"]["reserve_price_num"]["evidence"] == "VERIFIED"
    assert prov["1"]["village"]["page"] == 1
    assert prov["2"]["full_description"]["page"] == 2 and prov["2"]["full_description"]["start"] == 30
    assert prov["2"]["borrower"]["entity_id"] == "old"       # a v1 entity still has a span and an id
