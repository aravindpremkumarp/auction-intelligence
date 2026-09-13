"""build_spine's pure half: clustering over the bridge edges (INFERRED never
merges), the fetched lot map → the merge's lot shape, one event per
cluster with its LISTS / ANNOUNCES / DEPICTS targets, and the summary.
"""
from __future__ import annotations

import json

from scripts import build_spine as bs


def _row(aid, source, neighbours=(), **extra):
    return {"auction_id": aid, "source": source, "source_rank": {"baanknet": 1, "bankeauctions": 2}.get(source, 3),
            "title": "t", "description": "", "bank": "Indian Overseas Bank", "reserve_price_num": 4626500.0,
            "auction_start_dt": "2026-09-24T11:00:00", "neighbours": list(neighbours), "media": [], "documents": [],
            "lot": None, **extra}


def test_cluster_listings_merges_confirmed_and_probable_only():
    rows = [
        _row("841207", "eauctionsindia", [{"id": "bn-1", "confidence": "PROBABLE"}, {"id": "be-9", "confidence": "INFERRED"}]),
        _row("bn-1", "baanknet", [{"id": "841207", "confidence": "PROBABLE"}, {"id": "be-1", "confidence": "CONFIRMED"}]),
        _row("be-1", "bankeauctions", [{"id": "bn-1", "confidence": "CONFIRMED"}]),
        _row("be-9", "bankeauctions", [{"id": "841207", "confidence": "INFERRED"}]),
        _row("900000", "eauctionsindia", [{"id": "gone", "confidence": "CONFIRMED"}]),   # neighbour not fetched
    ]
    clusters = bs.cluster_listings(rows)
    assert [[r["auction_id"] for r in c] for c in clusters] == [["841207", "be-1", "bn-1"], ["900000"], ["be-9"]]
    assert [bs.cluster_confidence(c) for c in clusters] == ["PROBABLE", "SINGLE", "SINGLE"]
    assert bs.cluster_confidence(clusters[0][1:]) == "CONFIRMED"        # be-1 ~ bn-1 alone


def test_lot_from_record_shapes_sides_and_headline_extent():
    lot = {"lot_key": "f.png#1", "property_type": "house", "district": "Tirunelveli", "possession": "physical",
           "bounds": [["north", "Road", 30.0], ["south", "House of Kumar", None], ["north", "dup", 99.0]],
           "extents": [["uds_parent", 9000.0, "9000 sq.ft", False], ["built_up", 1215.0, "1215 sq.ft", True]]}
    out = bs.lot_from_record(lot)
    assert out["boundaries"] == {"north": "Road", "south": "House of Kumar"}
    assert out["measurements"] == {"north": 30.0}
    assert (out["extent_sqft"], out["extent_kind"], out["extent_raw"]) == (1215.0, "built_up", "1215 sq.ft")
    assert "bounds" not in out and out["possession"] == "physical"
    assert bs.lot_from_record(None) is None and bs.lot_from_record({"lot_key": None}) is None
    no_headline = bs.lot_from_record({"lot_key": "k", "extents": [["total", 500.0, "500 sq.ft", None]]})
    assert no_headline["extent_sqft"] == 500.0


def test_build_events_one_per_cluster_with_edge_targets():
    rows = [
        _row("841207", "eauctionsindia", [{"id": "bn-1", "confidence": "PROBABLE"}],
             documents=["ib1.png"], revenue_district="Tirunelveli", property_type_effective="house",
             lot={"lot_key": "ib1.png#1", "property_type": "house", "district": "Tirunelveli", "possession": "physical",
                  "bounds": [], "extents": [["total", 1215.0, "1215 sq.ft", True]]}),
        _row("bn-1", "baanknet", [{"id": "841207", "confidence": "PROBABLE"}], portal_district="Tirunelveli",
             possession_type="symbolic", auction_status="live", documents=["bn-379330.pdf"],
             media=[{"url": "https://cdn/1.jpg", "kind": "image", "is_main": True}, {"url": "https://cdn/1.jpg", "kind": "image"}]),
        _row("be-9", "bankeauctions"),
    ]
    events = bs.build_events(rows, built_at="t")
    assert [e["listing_ids"] for e in events] == [["841207", "bn-1"], ["be-9"]]
    merged, single = events
    assert merged["props"]["event_id"] == merged["props"]["event_id"] and merged["props"]["event_id"].startswith("ev-")
    assert merged["documents"] == ["bn-379330.pdf", "ib1.png"] and merged["media_urls"] == ["https://cdn/1.jpg"]
    assert merged["props"]["possession_type"] == "physical"          # notice over portal
    assert merged["props"]["auction_status"] == "live"               # portal over notice
    assert merged["props"]["has_photos"] is True and merged["props"]["confidence"] == "PROBABLE"
    assert json.loads(merged["props"]["provenance_json"])["extent_sqft"] == "notice:ib1.png#1"
    assert single["props"]["event_id"] == "ev-be-9" and single["props"]["confidence"] == "SINGLE"

    text = bs.summarize(events)
    assert text.startswith("events: 2  (from 3 listings)")
    assert "2 listings: 1" in text and "with photos 1" in text
    assert bs.summarize([]).endswith("(no events)")
