"""
tests/scripts/test_tn_prepare_and_keys.py
------------------------------------------
Covers the two data-side halves of the silent-edge-loss bug:

1. prepare_tn_data read the JSONL key ``Auction Type`` while newer scrapes
   write ``AuctionType``. Those records reached the loader with an empty
   auction_type, which is what tripped the Cypher cascade.
2. upload_tn_to_r2 mapped filename -> auction_id last-writer-wins, so
   regenerating the JSONL moved a shared notice to a new key and uploaded a
   byte-identical copy beside the old one.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ── prepare_tn_data: both spellings of the auction-type key ──────────────────

def _record(auction_id: str, **over) -> dict:
    rec = {
        "URL": f"https://www.eauctionsindia.com/properties/{auction_id}",
        "Title": "T", "Description": "D",
        "Province/State": "Tamil Nadu", "City/Town": "Chennai", "Area/Town": "Adyar",
        "Bank Name": "SBI", "Branch Name": "Adyar", "Borrower Name": "Someone",
        "Asset Category": "Residential", "Property Type": "Flat",
        "Reserve Price": "", "EMD": "",
        "Auction Start Date": "", "Auction End Time": "",
        "Application Subbmision Date": "",
        "Downloads": "N/A",
    }
    rec.update(over)
    return rec


def _run_prepare(tmp_path: Path, records: list[dict]) -> list[dict]:
    import scripts.prepare_tn_data as prep

    src = tmp_path / "in.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    out = tmp_path / "out.jsonl"
    dl = tmp_path / "downloads"
    dl.mkdir()

    orig = (prep.INPUT_FILE, prep.OUTPUT_FILE, prep.REPORT_FILE, prep.DL_DIR)
    prep.INPUT_FILE = str(src)
    prep.OUTPUT_FILE = str(out)
    prep.REPORT_FILE = str(tmp_path / "report.txt")
    prep.DL_DIR = str(dl)
    try:
        prep.main()
    finally:
        prep.INPUT_FILE, prep.OUTPUT_FILE, prep.REPORT_FILE, prep.DL_DIR = orig

    return [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_reads_legacy_spaced_auction_type_key(tmp_path):
    rows = _run_prepare(tmp_path, [_record("1", **{"Auction Type": "SARFAESI Auction"})])
    assert rows[0]["auction_type"] == "SARFAESI Auction"


def test_reads_current_unspaced_auction_type_key(tmp_path):
    """The regression: this spelling used to yield '' and cost the Borrower edge."""
    rows = _run_prepare(tmp_path, [_record("2", **{"AuctionType": "DRT Auction"})])
    assert rows[0]["auction_type"] == "DRT Auction"


def test_missing_auction_type_stays_empty(tmp_path):
    rows = _run_prepare(tmp_path, [_record("3")])
    assert rows[0]["auction_type"] == ""


def test_borrower_is_carried_regardless_of_auction_type(tmp_path):
    rows = _run_prepare(tmp_path, [_record("4")])
    assert rows[0]["borrower_name"] == "Someone"


# ── upload_tn_to_r2: one object per filename, stable across rebuilds ─────────

@pytest.fixture(scope="module")
def up():
    pytest.importorskip("boto3")
    import os
    for var, val in [("R2_ACCOUNT_ID", "x"), ("R2_ACCESS_KEY_ID", "x"),
                     ("R2_SECRET_ACCESS_KEY", "x"), ("R2_BUCKET", "b"),
                     ("R2_PUBLIC_BASE_URL", "https://r2.example")]:
        os.environ.setdefault(var, val)
    import scripts.upload_tn_to_r2 as mod
    return mod


def test_shared_notice_reuses_the_existing_key(up):
    """A file already in R2 is never uploaded again under a different auction."""
    plan = up.plan_uploads(
        ["batch.pdf"],
        {"batch.pdf": "999"},                  # map says it belongs to 999...
        {"notices/100/batch.pdf"},             # ...but it already lives under 100
    )
    assert plan == [("batch.pdf", "notices/100/batch.pdf", "reuse")]


def test_new_file_uploads_under_its_mapped_auction(up):
    plan = up.plan_uploads(["new.pdf"], {"new.pdf": "500"}, set())
    assert plan == [("new.pdf", "notices/500/new.pdf", "upload")]


def test_unmapped_file_falls_back(up):
    plan = up.plan_uploads(["stray.pdf"], {}, set())
    assert plan == [("stray.pdf", "notices/tn_unknown/stray.pdf", "upload")]


def test_choice_is_stable_when_several_copies_exist(up):
    """Duplicates already in R2 must not make the winner depend on set order."""
    existing = {"notices/300/d.pdf", "notices/100/d.pdf", "notices/200/d.pdf"}
    first = up.plan_uploads(["d.pdf"], {"d.pdf": "300"}, existing)
    again = up.plan_uploads(["d.pdf"], {"d.pdf": "100"}, set(reversed(sorted(existing))))
    assert first == again == [("d.pdf", "notices/100/d.pdf", "reuse")]


def test_filename_map_is_order_independent(up, tmp_path, monkeypatch):
    """Lowest auction_id wins, so a JSONL rebuild cannot move a shared notice."""
    shared = "batch.pdf"
    recs = [{"auction_id": a, "downloads_list": [shared]} for a in ("300", "100", "200")]

    def _write(order):
        p = tmp_path / f"{'-'.join(order)}.jsonl"
        by_id = {r["auction_id"]: r for r in recs}
        p.write_text("\n".join(json.dumps(by_id[a]) for a in order), encoding="utf-8")
        return p

    monkeypatch.setattr(up, "JSONL_FILE", _write(["300", "100", "200"]))
    forward = up.build_filename_map()
    monkeypatch.setattr(up, "JSONL_FILE", _write(["200", "300", "100"]))
    shuffled = up.build_filename_map()

    assert forward == shuffled == {shared: "100"}
