"""A place named only as 'Perambur' yields no district or state."""
from __future__ import annotations

from pipeline.reader.convert import to_entities
from pipeline.reader.schema import LotRead
from pipeline.reader.segment import Segment

MD = ("Sale notice. Reserve Price Rs.50,00,000/- EMD Rs.5,00,000/-. All that piece and parcel "
      "of the house bearing Door No 5 situated at Perambur, measuring 1200 sq.ft, bounded on the "
      "north by road, south by plot 6, east by plot 4, west by lane. Auction on 15-10-2026.")


def test_district_and_state_are_not_invented():
    lot = LotRead.model_validate({
        "reserve_price": {"status": "found", "quote": "Rs.50,00,000/-"},
        "emd": {"status": "found", "quote": "Rs.5,00,000/-"},
        "location": {"quote": "situated at Perambur", "city": "Perambur",
                     "district": "Chennai", "state": "Tamil Nadu"},
        "identifiers": [{"kind": "door_old", "value": "5", "quote": "Door No 5"}],
        "extents": [{"role": "total_area", "quote": "1200 sq.ft"}],
        "description": {"first_words": "All that piece and parcel", "last_words": "west by lane."},
        "dates": [{"status": "found", "quote": "Auction on 15-10-2026", "event": "auction_start"}],
    })
    conv = to_entities(None, [(Segment(0, len(MD), 0), lot)], MD)
    loc = next(e for e in conv.entities if e["cls"] == "location")
    assert loc["attrs"]["city"] == "Perambur"
    assert "district" not in loc["attrs"] and "state" not in loc["attrs"]
    assert loc["attrs"]["inferred_dropped"] == "district,state"
    t = next(e for e in conv.entities if e["cls"] == "auction_terms")
    assert t["attrs"]["auction_start_dt"] == "2026-10-15"
