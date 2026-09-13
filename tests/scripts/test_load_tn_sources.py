"""The loader on multi-source input: a legacy row and a harvested row must
both come out with the source properties docs/SCHEMA.md lists, the insert
filter admits a photo-only listing, inputs resolve to the listings directory
first, and --dry-run never opens a connection.
"""
from __future__ import annotations

import json

import pytest

from scripts import load_tn_to_neo4j as ld

LEGACY_ROW = {  # the shape prepare_tn_data has always written
    "auction_id": "841207", "url": "https://www.eauctionsindia.com/auction/841207", "title": "t",
    "description": "d", "reserve_price_raw": "₹46,26,500", "reserve_price_num": 4626500.0,
    "auction_start_dt": "2026-09-24T11:00:00", "bank_name": "Indian Overseas Bank", "state": "Tamil Nadu",
    "city": "Tirunelveli", "downloads_list": ["ib17875672725500.png"], "downloads_found": ["ib17875672725500.png"],
    "downloads_missing": [], "downloads_complete": True, "property_types": ["House"],
}

PORTAL_ROW = {  # bn-359826 from the 2026-09-12 harvest, trimmed
    "auction_id": "bn-359826", "url": "https://baanknet.com/property-detail/119851", "title": "t", "description": "d",
    "reserve_price_raw": "4626500.00000", "reserve_price_num": 4626500.0, "auction_start_dt": "2026-09-24T11:00:00",
    "bank_name": "Indian Overseas Bank", "state": "Tamil Nadu", "city": "", "property_types": ["House"],
    "downloads_list": ["bn-379330.pdf"], "downloads_found": ["bn-379330.pdf"], "downloads_missing": [], "downloads_complete": True,
    "source": "baanknet", "source_id": "359826", "source_url": "https://baanknet.com/property-detail/119851", "source_rank": 1,
    "district": "Tirunelveli", "pincode": "627357", "possession_type": "symbolic", "auction_status": "live",
    "inspection_start_dt": "2026-09-11T15:05:00", "inspection_end_dt": "2026-09-23T17:00:00",
    "documents": [{"url": "https://cdn.baanknet.com/x/379330.pdf", "filename": "bn-379330.pdf", "label": "SALE NOTICE",
                   "doc_role": "sale_notice", "needs_referer": False, "referer": None}],
    "media": [{"url": "https://cdn.baanknet.com/x/119740.jpg", "kind": "image", "is_main": True, "label": None},
              {"url": "https://cdn.baanknet.com/x/v.mp4", "kind": "video", "is_main": False, "label": None}],
    "has_photos": True, "fetched_at": "2026-09-12T14:29:44+00:00",
}


def test_legacy_row_is_eauctionsindia_rank_3():
    r = ld.sanitise(LEGACY_ROW)
    assert (r["source"], r["source_rank"], r["source_id"], r["source_url"]) == (
        "eauctionsindia", 3, "841207", "https://www.eauctionsindia.com/auction/841207")
    assert r["media"] == [] and r["photo_urls"] == [] and r["document_roles"] == []
    assert r["fetched_at"] is None and r["portal_district"] is None


def test_portal_row_keeps_its_source_and_media():
    r = ld.sanitise(PORTAL_ROW)
    assert (r["source"], r["source_rank"], r["source_id"]) == ("baanknet", 1, "359826")
    assert (r["portal_district"], r["pincode"], r["possession_type"], r["auction_status"]) == (
        "Tirunelveli", "627357", "symbolic", "live")
    assert r["document_roles"] == ["sale_notice"] and r["document_urls"] == ["https://cdn.baanknet.com/x/379330.pdf"]
    assert [m["kind"] for m in r["media"]] == ["image", "video"] and r["media"][0]["is_main"] is True
    assert r["photo_urls"] == ["https://cdn.baanknet.com/x/119740.jpg"]
    assert r["bank_short_name"]        # every Bank node still gets a short name


def test_insert_filter_admits_a_document_or_a_photo():
    assert ld.is_loadable(LEGACY_ROW)
    assert ld.is_loadable({"downloads_found": [], "media": [{"url": "https://cdn/p.jpg", "kind": "image"}]})
    assert not ld.is_loadable({"downloads_found": [], "media": []})
    assert not ld.is_loadable({})


def test_batch_query_sets_source_props_and_merges_media():
    q = ld.BATCH_QUERY
    for prop in ("source", "source_id", "source_url", "source_rank", "fetched_at", "last_seen_at", "portal_district",
                 "pincode", "possession_type", "extent_raw", "inspection_start_dt", "auction_status",
                 "document_roles", "document_urls", "photo_urls"):
        assert f"a.{prop} " in q, prop
    assert "MERGE (md:Media {url: m.url})" in q and "MERGE (a)-[:HAS_MEDIA]->(md)" in q
    # the backfill only touches nodes that predate the adapters
    assert "WHERE a.source IS NULL" in ld.BACKFILL_SOURCE_QUERY


def test_resolve_inputs_prefers_listings_then_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(ld, "LISTINGS_GLOB", str(tmp_path / "listings" / "*.jsonl"))
    monkeypatch.setattr(ld, "INPUT_FILE", str(tmp_path / "tn_auction_data.jsonl"))
    assert ld.resolve_inputs(None) == []                      # nothing anywhere
    (tmp_path / "tn_auction_data.jsonl").write_text("{}\n")
    assert [p.name for p in ld.resolve_inputs(None)] == ["tn_auction_data.jsonl"]
    (tmp_path / "listings").mkdir()
    for n in ("bankeauctions", "baanknet"):
        (tmp_path / "listings" / f"{n}.jsonl").write_text("{}\n")
    assert [p.name for p in ld.resolve_inputs(None)] == ["baanknet.jsonl", "bankeauctions.jsonl"]
    assert [p.name for p in ld.resolve_inputs([str(tmp_path / "listings" / "ba*.jsonl")])] == ["baanknet.jsonl", "bankeauctions.jsonl"]
    assert ld.resolve_inputs([str(tmp_path / "nope" / "*.jsonl")]) == []   # an explicit miss never falls back


def test_load_inputs_dedupes_on_auction_id_later_file_wins(tmp_path):
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    a.write_text(json.dumps({"auction_id": "1", "title": "old"}) + "\n" + json.dumps({"auction_id": "2"}) + "\n")
    b.write_text(json.dumps({"auction_id": "1", "title": "new"}) + "\n")
    rows = ld.load_inputs([a, b])
    assert sorted(r["auction_id"] for r in rows) == ["1", "2"]
    assert next(r for r in rows if r["auction_id"] == "1")["title"] == "new"


def test_dry_run_never_connects(tmp_path, monkeypatch, capsys):
    path = tmp_path / "baanknet.jsonl"
    path.write_text(json.dumps(PORTAL_ROW) + "\n" + json.dumps({**LEGACY_ROW, "downloads_found": []}) + "\n")

    def _boom(*a, **kw):
        raise AssertionError("dry-run must not open a driver")
    monkeypatch.setattr(ld.GraphDatabase, "driver", _boom)

    assert ld.main(["--input", str(path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "2 records in file(s); 1 with a document or photo (baanknet 1)" in out
    assert '"source": "baanknet"' in out


def test_main_fails_when_no_input_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(ld, "LISTINGS_GLOB", str(tmp_path / "*.jsonl"))
    monkeypatch.setattr(ld, "INPUT_FILE", str(tmp_path / "missing.jsonl"))
    assert ld.main(["--dry-run"]) == 1


@pytest.mark.parametrize("source, rank", [("eauctionsindia", 3), ("bankeauctions", 2), ("baanknet", 1)])
def test_source_rank_defaults_from_the_registry(source, rank):
    assert ld.sanitise({"auction_id": "x", "source": source})["source_rank"] == rank
