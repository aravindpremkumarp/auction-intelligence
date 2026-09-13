"""The eauctionsindia adapter must produce exactly what ``prepare_tn_data``
always produced — the loader downstream is unchanged and reads those keys.

The regression test runs the retired self-contained script
(``scripts/legacy/prepare_tn_data_v1.py``) and the new adapter-backed one on
the same input and compares every legacy key, row for row.
"""
from __future__ import annotations

import importlib
import json

import pytest

from sources.base import LEGACY_ROW_KEYS
from sources.eauctionsindia import EauctionsIndiaAdapter, split_downloads

# A record in the shape phase2 writes — real key names, including the two
# drifted spellings, and the '_' bookkeeping keys.
TN_ROW = {
    "URL": "https://www.eauctionsindia.com/properties/841207",
    "Title": "Residential House at Chennai",
    "Bank Name": "State Bank Of India",
    "Branch Name": "SARB Chennai",
    "Borrower Name": "Mr. B. Sridhar",
    "ReservePrice": "₹43,60,000",              # newer spelling
    "EMD": "₹4,36,000",
    "Province/State": "Tamil Nadu",
    "City/Town": "Chennai",
    "Area/Town": "Ambattur",
    "Asset Category": "Residential",
    "Property Type": "House, Land",
    "AuctionType": "e-Auction",                 # newer spelling
    "Auction Start Date": "17-09-2026 1100 AM",
    "Auction End Time": "17-09-2026 0100 PM",
    "Application Subbmision Date": "16-09-2026 0500 PM",   # the old typo key
    "Service Provider": "MSTC",
    "Contact Details": "044-000000",
    "Description": "House on plot 12 Province/State : Tamil Nadu City/Town : Chennai",
    "Downloads": "SBI17819383495370.pdf; SBI17819383495371.jpg",
    "downloads_list": ["SBI17819383495370.pdf", "SBI17819383495371.jpg"],
    "_scraped_at": "2026-09-12T08:00:00",
    "_worker": 3,
}


@pytest.fixture
def adapter(tmp_path):
    (tmp_path / "SBI17819383495370.pdf").write_bytes(b"%PDF")
    return EauctionsIndiaAdapter(input_path=tmp_path / "in.jsonl", download_dir=tmp_path)


def test_normalize_maps_every_legacy_field(adapter):
    row = adapter.normalize(TN_ROW).to_row()

    assert row["auction_id"] == "841207"            # bare — no prefix
    assert row["source"] == "eauctionsindia" and row["source_rank"] == 3
    assert row["source_id"] == "841207"
    assert row["reserve_price_num"] == 4360000.0 and row["reserve_price_raw"] == "₹43,60,000"
    assert row["emd_num"] == 436000.0
    assert row["auction_start_dt"] == "2026-09-17T11:00:00"
    assert row["auction_end_dt"] == "2026-09-17T13:00:00"
    assert row["application_deadline_dt"] == "2026-09-16T17:00:00"
    assert row["application_deadline_raw"] == "16-09-2026 0500 PM"
    assert row["property_types"] == ["House", "Land"]
    assert row["auction_type"] == "e-Auction"
    assert row["description"] == "House on plot 12"    # field bleed cut
    assert row["downloads_list"] == ["SBI17819383495370.pdf", "SBI17819383495371.jpg"]
    assert row["downloads_found"] == ["SBI17819383495370.pdf"]
    assert row["downloads_missing"] == ["SBI17819383495371.jpg"]
    assert row["downloads_complete"] is False
    assert row["has_photos"] is False
    assert [d["doc_role"] for d in row["documents"]] == ["unknown", "unknown"]
    assert row["fetched_at"] == "2026-09-12T08:00:00"


def test_legacy_key_order_is_preserved(adapter):
    row = adapter.normalize(TN_ROW).to_row()
    assert tuple(row)[: len(LEGACY_ROW_KEYS)] == LEGACY_ROW_KEYS


def test_reads_the_older_key_spellings_too(adapter):
    old = dict(TN_ROW)
    del old["ReservePrice"]
    del old["AuctionType"]
    del old["Application Subbmision Date"]
    old["Reserve Price"] = "₹43,60,000"
    old["Auction Type"] = "e-Auction"
    old["Application Submission Date"] = "16-09-2026 0500 PM"

    new_row = adapter.normalize(TN_ROW).to_row()
    old_row = adapter.normalize(old).to_row()
    assert old_row == new_row


def test_other_states_and_unwanted_categories_are_dropped(adapter):
    assert adapter.normalize({**TN_ROW, "Province/State": "Karnataka"}) is None
    assert adapter.normalize({**TN_ROW, "Asset Category": "Vehicle Auctions"}) is None
    assert adapter.normalize({**TN_ROW, "Asset Category": "Gold Auctions"}) is None
    # substring match, as before: "TAMIL NADU" and "Tamil Nadu, India" both pass
    assert adapter.normalize({**TN_ROW, "Province/State": "TAMIL NADU, India"}) is not None


def test_harvest_skips_blank_and_malformed_lines(adapter):
    adapter.input_path.write_text(
        json.dumps(TN_ROW) + "\n\n{not json}\n" + json.dumps({**TN_ROW, "URL": "https://x/properties/1"}) + "\n",
        encoding="utf-8",
    )
    ids = [r["URL"].rsplit("/", 1)[-1] for r in adapter.harvest()]
    assert ids == ["841207", "1"]
    assert [r["URL"].rsplit("/", 1)[-1] for r in adapter.harvest(limit=1)] == ["841207"]


def test_split_downloads():
    assert split_downloads("a.pdf; b.jpg, N/A") == ["a.pdf", "b.jpg"]
    assert split_downloads("N/A") == []
    assert split_downloads(None) == []


# ── regression against the retired script ──────────────────────────────────

def _run(module_name, tmp_path, rows, monkeypatch):
    mod = importlib.import_module(module_name)
    inp = tmp_path / f"{module_name.rsplit('.', 1)[-1]}_in.jsonl"
    out = tmp_path / f"{module_name.rsplit('.', 1)[-1]}_out.jsonl"
    rpt = tmp_path / f"{module_name.rsplit('.', 1)[-1]}_report.txt"
    inp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    monkeypatch.setattr(mod, "INPUT_FILE", str(inp))
    monkeypatch.setattr(mod, "OUTPUT_FILE", str(out))
    monkeypatch.setattr(mod, "REPORT_FILE", str(rpt))
    monkeypatch.setattr(mod, "DL_DIR", str(tmp_path))
    if hasattr(mod, "LISTINGS_FILE"):
        monkeypatch.setattr(mod, "LISTINGS_FILE", str(tmp_path / "listings" / "eauctionsindia.jsonl"))
    mod.main()
    return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()], rpt.read_text(encoding="utf-8")


def test_new_script_matches_legacy_on_every_legacy_key(tmp_path, monkeypatch, capsys):
    (tmp_path / "SBI17819383495370.pdf").write_bytes(b"%PDF")
    rows = [
        TN_ROW,
        {**TN_ROW, "URL": "https://www.eauctionsindia.com/properties/766811", "Reserve Price": "₹1,19,600",
         "ReservePrice": None, "Downloads": "N/A", "Description": None or "", "Property Type": ""},
        {**TN_ROW, "Province/State": "Kerala"},
        {**TN_ROW, "Asset Category": "Others"},
    ]
    legacy_rows, legacy_report = _run("scripts.legacy.prepare_tn_data_v1", tmp_path, rows, monkeypatch)
    new_rows, new_report = _run("scripts.prepare_tn_data", tmp_path, rows, monkeypatch)

    assert len(legacy_rows) == len(new_rows) == 2
    for old, new in zip(legacy_rows, new_rows):
        assert {k: new[k] for k in LEGACY_ROW_KEYS} == old
        assert set(new) > set(old)                      # strictly a superset
    assert new_report == legacy_report
    assert (tmp_path / "listings" / "eauctionsindia.jsonl").read_text(encoding="utf-8").count("\n") == 2
