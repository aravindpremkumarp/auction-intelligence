"""The merge: notice beats portal for what the property is, the portal beats
the notice for what the auction is doing, prices are graded not averaged,
one photo anywhere makes ``has_photos``, and the same listings always name
the same event.
"""
from __future__ import annotations

import json

import pytest

from sources.merge import (
    CORE_FIELDS, event_id_for, event_node_props, listing_branch, merge_event, notice_branch, pick_price,
)

BN = {  # bn-359826 as the graph holds it after Task 8
    "auction_id": "bn-359826", "source": "baanknet", "source_rank": 1, "source_url": "https://baanknet.com/property-detail/119851",
    "title": "D no 81A BY 1, S.No.381 BY 5A, naranammalpuram village, tirunelveli, total extent 1215 sqft",
    "description": "", "reserve_price_num": 4626500.0, "emd_num": None, "auction_start_dt": "2026-09-24T11:00:00",
    "auction_end_dt": "2026-09-24T13:00:00", "auction_status": "live", "possession_type": "symbolic",
    "portal_district": "Tirunelveli", "pincode": "627357", "property_types": ["House"], "bank": "Indian Overseas Bank",
    "borrower": "N MARIAPPAN", "extent_raw": "",
}
EI = {  # its eauctionsindia copy, with the notice applied by apply_extractions
    "auction_id": "841207", "source": "eauctionsindia", "source_rank": 3, "url": "https://www.eauctionsindia.com/auction/841207",
    "title": "House at Tirunelveli", "description": "Property of N. Mariappan, S.No 381/5A, bounded North by road, "
    "South by house of Kumar, East by street, West by house of Raman", "reserve_price_num": 4650000.0, "emd_num": 462650.0,
    "auction_start_dt": "2026-09-24T11:00:00", "auction_status": None, "city": "Tirunelveli", "property_types": ["House"],
    "bank": "Indian Overseas Bank", "borrower": "Mr. N. Mariappan",
    "revenue_district": "Tirunelveli", "property_type_effective": "house", "total_area": "1215 sq.ft",
    "boundary_north": "Road", "boundary_south": "House of Kumar", "boundary_east": "Street", "boundary_west": "House of Raman",
    "boundary_measurement_north": "30 ft", "boundary_measurement_south": "30 ft",
}
LOT = {
    "lot_key": "ib17875672725500.png#1", "property_type": "house", "district": "Tirunelveli", "extent_sqft": 1215.0,
    "extent_kind": "total", "extent_raw": "1215 sq.ft", "possession": "physical", "borrower": "N. Mariappan",
    "boundaries": {"north": "Road", "south": "House of Kumar", "east": "Street", "west": "House of Raman"},
    "measurements": {"north": 30.0, "south": 30.0, "east": 40.5, "west": 40.5},
    "reserve_price_num": 4626500.0, "emd_num": 462650.0, "auction_start_dt": "2026-09-24T11:00:00",
    "full_description": "All that piece and parcel …", "encumbrance": "nil", "attempt_no": 2,
}
MEDIA = [{"url": "https://cdn.baanknet.com/x/119740.jpg", "kind": "image", "is_main": True},
         {"url": "https://cdn.baanknet.com/x/v.mp4", "kind": "video", "is_main": False}]


def test_notice_wins_property_facts_and_portal_wins_lifecycle():
    ev = merge_event({"listings": [BN, EI], "lots": [LOT], "media": MEDIA, "confidence": "PROBABLE"})
    p = ev["provenance"]
    assert (ev["possession_type"], p["possession_type"]) == ("physical", "notice:ib17875672725500.png#1")   # not the portal's "symbolic"
    assert (ev["extent_sqft"], ev["extent_kind"], p["extent_sqft"]) == (1215.0, "total", "notice:ib17875672725500.png#1")
    assert ev["boundaries"]["north"] == "Road" and p["boundaries"].startswith("notice:")
    assert ev["measurements"] == {"north": 30.0, "south": 30.0, "east": 40.5, "west": 40.5}
    assert (ev["auction_status"], p["auction_status"]) == ("live", "baanknet:bn-359826")
    assert (ev["auction_end_dt"], p["auction_end_dt"]) == ("2026-09-24T13:00:00", "baanknet:bn-359826")
    assert (ev["pincode"], p["pincode"]) == ("627357", "baanknet:bn-359826")       # only the portal has it
    assert ev["encumbrance"] == "nil" and ev["description"] == "All that piece and parcel …"
    assert ev["sources"] == ["baanknet", "eauctionsindia", "notice"]
    assert ev["confidence"] == "PROBABLE" and ev["listing_ids"] == ["841207", "bn-359826"]


def test_prices_are_graded_not_averaged():
    ev = merge_event({"listings": [BN, EI], "lots": [LOT], "media": []})
    # BAANKNET (rank 1) is the portal figure; it agrees with the notice → the live portal figure, CONFIRMED-style
    assert (ev["reserve_price_num"], ev["provenance"]["reserve_price_num"], ev["reserve_price_agreement"]) == (
        4626500.0, "baanknet:bn-359826", "agree")
    # EMD: BAANKNET has none, eauctionsindia's 462650 agrees with the notice
    assert (ev["emd_num"], ev["emd_agreement"]) == (462650.0, "agree")

    # a portal figure off by a factor of ten keeps the notice's, flagged
    wrong = {**BN, "reserve_price_num": 462650.0}
    ev = merge_event({"listings": [wrong], "lots": [LOT], "media": []})
    assert (ev["reserve_price_num"], ev["provenance"]["reserve_price_num"], ev["reserve_price_agreement"]) == (
        4626500.0, "notice:ib17875672725500.png#1", "magnitude_slip")
    # portal only → its figure, agreement unknown
    ev = merge_event({"listings": [BN], "lots": [], "media": []})
    assert (ev["reserve_price_num"], ev["reserve_price_agreement"]) == (4626500.0, "unknown")


def test_has_photos_needs_one_image_and_core_counts_nine():
    full = merge_event({"listings": [BN, EI], "lots": [LOT], "media": MEDIA})
    assert (full["has_photos"], full["photo_count"], full["video_count"]) == (True, 1, 1)
    assert full["core_complete"] == 9 and full["core_missing"] == []
    assert full["provenance"]["has_photos"] == "media"

    video_only = merge_event({"listings": [BN, EI], "lots": [LOT], "media": [MEDIA[1]]})
    assert video_only["has_photos"] is False and video_only["core_complete"] == 8 and video_only["core_missing"] == ["has_photos"]

    portal_only = merge_event({"listings": [BN], "lots": [], "media": MEDIA})
    # portal: type, district, price, date, photos, possession; the title states the extent; no measurement, no boundaries
    assert portal_only["core_complete"] == 7
    assert portal_only["core_missing"] == ["measurement", "boundaries"]
    assert portal_only["confidence"] == "SINGLE"
    assert set(CORE_FIELDS) >= set(portal_only["core_missing"])


def test_notice_applied_to_the_listing_counts_when_no_lot_is_loaded():
    """apply_extractions wrote boundaries / district / total_area onto the
    listing; without the :Lot in the cluster those still form a notice branch."""
    ev = merge_event({"listings": [EI], "lots": [], "media": []})
    assert ev["provenance"]["boundaries"] == "notice:applied"
    assert ev["district"] == "Tirunelveli" and ev["provenance"]["district"] == "notice:applied"
    assert ev["measurements"] == {"north": "30 ft", "south": "30 ft"}
    assert ev["extent_raw"] == "1215 sq.ft"
    assert ev["core_complete"] == 7 and ev["core_missing"] == ["possession_type", "has_photos"]


def test_event_id_is_deterministic_and_order_free():
    assert event_id_for(["bn-359826", "841207"]) == event_id_for(["841207", "bn-359826"])
    assert event_id_for(["841207"]) == "ev-841207"
    assert event_id_for(["bn-359826", "841207"]).startswith("ev-") and len(event_id_for(["a", "b"])) == 19
    assert event_id_for(["a", "b"]) != event_id_for(["a", "c"])
    with pytest.raises(ValueError):
        event_id_for([])
    a = merge_event({"listings": [BN, EI], "lots": [LOT], "media": MEDIA}, built_at="t")
    b = merge_event({"listings": [EI, BN], "lots": [LOT], "media": list(reversed(MEDIA))}, built_at="t")
    assert a == b


def test_branches_and_node_props():
    lb = listing_branch(BN)
    assert (lb["key"], lb["source"], lb["rank"]) == ("baanknet:bn-359826", "baanknet", 1)
    assert lb["fields"]["property_type"] == "House" and lb["fields"]["boundaries"] is None
    assert notice_branch({}, applied={}) is None
    nb = notice_branch(LOT)
    assert nb["key"] == "notice:ib17875672725500.png#1" and nb["fields"]["possession_type"] == "physical"
    assert pick_price("reserve_price_num", [lb]) == (4626500.0, "baanknet:bn-359826", "unknown")

    props = event_node_props(merge_event({"listings": [BN, EI], "lots": [LOT], "media": MEDIA}, built_at="t"))
    assert json.loads(props["boundaries_json"])["west"] == "House of Raman"
    assert json.loads(props["provenance_json"])["auction_status"] == "baanknet:bn-359826"
    assert "boundaries" not in props and "provenance" not in props
    assert props["listing_ids"] == ["841207", "bn-359826"] and props["core_complete"] == 9
