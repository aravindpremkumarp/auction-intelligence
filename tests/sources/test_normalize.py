"""Every case here is a value seen on a portal, not an invented one.

The eauctionsindia cases are the exact strings ``prepare_tn_data`` was
written against; the others are from the 2026-09-12 recon
(docs/source-recon-2026-09.md) and the two NIT bundles opened that day.
"""
from __future__ import annotations

import pytest

from sources.normalize import clean_price, doc_role_for, make_auction_id, parse_date


# ── clean_price ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, num", [
    ("₹1,23,456.00", 123456.0),
    ("â‚¹1,23,456.00", 123456.0),          # mojibake from a cp1252 terminal
    ("₹ 45,60,000", 4560000.0),
    ("4360000", 4360000.0),
    ("4626500.00000", 4626500.0),           # BAANKNET auctionPrice
    ("", None),
    ("N/A", None),
    ("Rs.11,19,600/-", None),               # trailing '/-' is not a number; the notice path handles it
])
def test_clean_price_returns_raw_and_number(raw, num):
    assert clean_price(raw) == (raw, num)


def test_clean_price_none_is_none():
    assert clean_price(None) == (None, None)


# ── parse_date: eauctionsindia ──────────────────────────────────────────────

@pytest.mark.parametrize("raw, iso", [
    ("23-03-2026 0130 PM", "2026-03-23T13:30:00"),
    ("23-03-2026 130 PM", "2026-03-23T13:30:00"),     # 3-digit HHMM
    ("01-01-2026 1200 AM", "2026-01-01T00:00:00"),
    ("01-01-2026 1200 PM", "2026-01-01T12:00:00"),
    ("17-09-2026 1100 am", "2026-09-17T11:00:00"),
])
def test_parse_date_portal_format(raw, iso):
    assert parse_date(raw) == iso


# ── parse_date: BAANKNET (UTC) → IST ────────────────────────────────────────

@pytest.mark.parametrize("raw, iso", [
    ("2026-09-17T05:30:00.000Z", "2026-09-17T11:00:00"),
    ("2026-05-13T17:30:00.000Z", "2026-05-13T23:00:00"),
    ("2026-09-11T05:54:14.000Z", "2026-09-11T11:24:14"),
    ("2026-09-17T11:00:00", "2026-09-17T11:00:00"),    # naive stays as-is
])
def test_parse_date_iso(raw, iso):
    assert parse_date(raw) == iso


# ── parse_date: bankeauctions ───────────────────────────────────────────────

@pytest.mark.parametrize("raw, iso", [
    ("15 Sep 2026 11:00", "2026-09-15T11:00:00"),
    ("12 Sep 2026", "2026-09-12T00:00:00"),
    ("07 Aug 2026 00:00", "2026-08-07T00:00:00"),
    ("3 Oct 2026", "2026-10-03T00:00:00"),
    ("22 September 2026 11:00", "2026-09-22T11:00:00"),
])
def test_parse_date_text(raw, iso):
    assert parse_date(raw) == iso


@pytest.mark.parametrize("raw", [None, "", "N/A", "none", "--", "soon", "31-02-2026 0100 PM", "2026-13-01T00:00:00"])
def test_parse_date_rejects_garbage_and_impossible_dates(raw):
    assert parse_date(raw) is None


# ── ids ─────────────────────────────────────────────────────────────────────

def test_make_auction_id():
    assert make_auction_id("bn-", 358394) == "bn-358394"
    assert make_auction_id("be-", "235813") == "be-235813"
    assert make_auction_id("", "841207") == "841207"
    with pytest.raises(ValueError):
        make_auction_id("bn-", "")


# ── document roles ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("label, role", [
    # the Omkara NIT bundle, 2026-09-12
    ("Account Teezle Telematics Tender Document.pdf", "tender"),
    ("Account Teezle Telematics Terms and Conditions of E auction.pdf", "terms"),
    ("Affidavit Cum Declaration 29A.pdf", "affidavit"),
    ("Omkara-Dinakaran-Chennai-23-08-2026.pdf", "publication"),
    ("Omkara-FE-Chennai-23-08-2026.pdf", "publication"),
    ("Property Details - TEEZLE TELEMATICS.pdf", "property_details"),
    ("Sale Proclamation dated 23.08.2026.pdf", "proclamation"),
    # the Hinduja bundle
    ("Md Mustafa Auction Notice with receipts.pdf", "sale_notice"),
    ("Vinothkumar P Auction Notice with receipts.pdf", "sale_notice"),
    # names from the 2026-09-12 --limit 20 harvest (Grihum, Dharmapuri bundles)
    ("BS-CHENNAI-PRUDENT-GRIHUM.pdf", "publication"),                       # Business Standard
    ("DK-COIMBATORE-GRIHUM-NIDO.pdf", "publication"),                        # Dinakaran
    ("20260817120710_The-New-Indian-Express-Dharmapuri-15-08-2026-page-6.pdf", "publication"),
    ("20260817122711_The-New-Indian-Express-Dharmapuri-15-08-2026-page-11 (1).pdf", "publication"),
    ("34534260_20260817120710_Thambi Theneer Sale.pdf", "sale_notice"),
    ("28220154_20260817122711_Vijay Tyres Sale.pdf", "sale_notice"),
    # bankeauctions detail-page labels
    ("Tender Documents", "tender"),
    ("Annexure 2/Details of Bidders", "unknown"),
    ("Annexure 3/Declaration by Bidders", "affidavit"),
    # BAANKNET auctionDocuments[].description
    ("SALE NOTICE", "sale_notice"),
    ("sale notice", "sale_notice"),
    ("Web sale notice", "sale_notice"),
    ("Sale Notice along with terms and conditions.", "terms"),
    ("TERMS AND CONDITIONS OF SALE", "terms"),
    ("Paper publication Chennai", "publication"),
    ("Paper publication English Bengaluru", "publication"),
    ("VICKY LIFE STYLE", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_doc_role_for(label, role):
    assert doc_role_for(label) == role
