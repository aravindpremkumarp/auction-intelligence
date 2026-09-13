"""bankeauctions adapter on the shapes recorded live on 2026-09-12.

``ROW`` is the real Omkara ARC datatable row (Chennai, id 239024, detail id
237860) — the auction whose NIT bundle held two newspaper publications and
a property-details PDF with photos. ``DETAIL_HTML`` reproduces that detail
page's markup for the fields the adapter reads, plus the document links and
the login captcha ``<img>`` that must never be taken for a photo.
"""
from __future__ import annotations

import io
import types
import zipfile

import pytest

from sources.bankeauctions import (
    BankeauctionsAdapter, detail_url_for, documents_from_detail, file_slug, parse_detail,
)

ROW = [
    "/public/uploads/bank/71c642d94e247649bc915f5699282a8d.jpg",
    "239024",
    "Omkara Assets Reconstruction Private Limited",
    "SCHEDULE A\nAll that piece and parcel of land being Plot Nos. 1,2,3,4,5 &6 in Bangaru Street, Saligramam",
    "Chennai",
    "15 Sep 2026",
    "11700000.00",
    "1170000.00",
    "SARFAESI",
    "--",
    "237860",
    "0",
    "Immovable",
    "Land",
    "0",
]

DETAIL_URL = "https://bankeauctions.com/immovable-land-chennai-237860"


def _pair(label, value):
    return (f'<div class="row"><div class="col-md-6 col-sm-5 detl-left">{label} :</div>'
            f'<div class="col-md-6 col-sm-7 detl-right">{value}</div></div>')


DETAIL_HTML = "\n".join([
    '<span id="captcha_cont"><img src="https://bankeauctions.com/public/uploads/images/1789201826.6292.jpg" width="120" height="39" alt=" " /></span>',
    '<div class="detl-heading text-center mb-3">Auction Details</div>',
    _pair("Organisation Name", "Omkara Assets Reconstruction Private Limited"),
    _pair("Event Branch", "Omkara ARC, Chennai"),
    _pair("Property Category", "Immovable"),
    _pair("Property Sub Category", "Land"),
    _pair("Property Description", "SCHEDULE A All that piece and parcel of land being Plot Nos. 1,2,3,4,5 &amp;6 in Bangaru Street, "
          "Saligramam and comprised in Old Survey No.116/1 Part and New T.S.No.179/1 Block No.42 "
          "measuring to an extent of 43626 Sq feet and bounded on the North by: T. S. No.97 &amp;179;/2"),
    _pair("Borrower's Name", "M/s. TEEZLE TELEMATICS INDIA PRIVATE LIMITED"),
    _pair("Reserve Price", "1,17,00,000.00"),
    _pair("Tender Fee", "0.00 "),
    _pair("Bid Increment value", "60,000.00"),
    _pair("Auto Extension time", "5 (In Minutes)"),
    '<div class="row"><div class="col-lg-12 text-center detl-hd2"><strong class="d-block">EMD Details</strong></div></div>',
    _pair("EMD Amount", "11,70,000.00"),
    _pair("EMD Deposit Bank Name", "Omkara ARC"),
    _pair("EMD Deposit Bank Account Number", "OMKARA0001"),
    _pair("EMD Deposit Bank IFSC Code", "HDFC0004989"),
    _pair("Press Release Date", "23 Aug 2026 00:00"),
    _pair("Date of Inspection of Property (From)", "01 Sep 2026 11:00"),
    _pair("Date of Inspection of Property (To)", "10 Sep 2026 16:00"),
    _pair("Offer (First Round Quote) Submission Last Date", "14 Sep 2026 17:00"),
    _pair("Auction Start Date and Time", "15 Sep 2026 11:00"),
    _pair("Auction End Date and Time", "15 Sep 2026 13:00"),
    '<div class="detl-heading">Auction Related Documents</div>',
    '<div class="row"><div class="col-md-6 col-sm-5 detl-left">View NIT Documents :</div>'
    '<div class="col-md-6 col-sm-7 detl-right"><a class="b_download" target="_blank" download="" '
    'href="/public/uploads/event_auction/93bb7ecd39ff1a76e7db007b839261c8.zip">Download</a></div></div>',
    '<div class="row"><div class="col-md-6 col-sm-5 detl-left">Tender Documents :</div>'
    '<div class="col-md-6 col-sm-7 detl-right"><a class="b_download" href="/public/uploads/bank/87ab7088c814a560c55974acdb53864b.pdf">Download</a></div></div>',
    '<div class="row"><div class="col-md-6 col-sm-5 detl-left">Annexure 2/Details of Bidders :</div>'
    '<div class="col-md-6 col-sm-7 detl-right"><a class="b_download" href="/public/uploads/bank/06fff19ca5917a1dc9ff0c97f3fa1227.pdf">Download</a></div></div>',
    '<div class="row"><div class="col-md-6 col-sm-5 detl-left">Annexure 3/Declaration by Bidders :</div>'
    '<div class="col-md-6 col-sm-7 detl-right"><a class="b_download" href="/public/uploads/bank/194dabdb57cf3acf3e2c25650c7fcf4a.pdf">Download</a></div></div>',
    '<a href="https://bankeauctions.com/public/uploads/Bank_E-Auctions_User_Agreement_and_Privacy_Policy.pdf">User agreement</a>',
    '<a href="https://bankeauctions.com/Terms___Condition.pdf">T&amp;C</a>',
])


def test_detail_url_is_the_sites_own_slug():
    assert detail_url_for(ROW) == DETAIL_URL
    assert detail_url_for([*ROW[:4], "Tirupathur (Vellore)", *ROW[5:10], "235813", *ROW[11:12], "Immovable", "Land and Building", "0"]) \
        == "https://bankeauctions.com/immovable-land-and-building-tirupathur-vellore-235813"


def test_parse_detail_reads_every_label_once():
    d = parse_detail(DETAIL_HTML)
    assert d["reserve"] == "1,17,00,000.00" and d["emd"] == "11,70,000.00"
    assert d["borrower"] == "M/s. TEEZLE TELEMATICS INDIA PRIVATE LIMITED"
    assert d["auction_start"] == "15 Sep 2026 11:00" and d["offer_deadline"] == "14 Sep 2026 17:00"
    assert d["description"].startswith("SCHEDULE A All that piece") and "&amp;" not in d["description"]
    assert d["emd_ifsc"] == "HDFC0004989"


def test_documents_bundle_first_then_loose_tender_pdfs_never_the_site_policies():
    docs = documents_from_detail(DETAIL_HTML, DETAIL_URL, "239024")
    assert [(d.filename, d.doc_role, d.needs_referer) for d in docs] == [
        ("be-239024-nit.zip", "bundle", True),
        ("be-239024-tender-documents.pdf", "tender", False),
        ("be-239024-annexure-2-details-of-bidders.pdf", "tender", False),
        ("be-239024-annexure-3-declaration-by-bidders.pdf", "affidavit", False),
    ]
    assert docs[0].referer == DETAIL_URL
    assert docs[0].url == "https://bankeauctions.com/public/uploads/event_auction/93bb7ecd39ff1a76e7db007b839261c8.zip"
    assert not any("User_Agreement" in d.url or "Terms___Condition" in d.url for d in docs)


def test_normalize_maps_row_and_detail():
    ad = BankeauctionsAdapter(state="Tamil Nadu")
    row = ad.normalize({"row": ROW, "detail_url": DETAIL_URL, "detail_html": DETAIL_HTML}).to_row()

    assert row["auction_id"] == "be-239024" and row["source_id"] == "239024"
    assert row["source"] == "bankeauctions" and row["source_rank"] == 2
    assert row["url"] == DETAIL_URL
    assert row["bank_name"] == "Omkara Assets Reconstruction Private Limited"
    assert row["branch_name"] == "Omkara ARC, Chennai"
    assert row["borrower_name"] == "M/s. TEEZLE TELEMATICS INDIA PRIVATE LIMITED"
    assert row["reserve_price_num"] == 11700000.0 and row["emd_num"] == 1170000.0
    assert row["bid_increment_num"] == 60000.0
    assert row["auction_start_dt"] == "2026-09-15T11:00:00" and row["auction_end_dt"] == "2026-09-15T13:00:00"
    assert row["application_deadline_dt"] == "2026-09-14T17:00:00"
    assert row["inspection_start_dt"] == "2026-09-01T11:00:00" and row["inspection_end_dt"] == "2026-09-10T16:00:00"
    assert row["city"] == "Chennai" and row["state"] == "Tamil Nadu"
    assert row["asset_category"] == "Residential" and row["property_types"] == ["Land"]
    assert row["auction_type"] == "SARFAESI Auction"
    assert "bounded on the North by" in row["description"]      # boundaries text kept whole
    assert row["contact_details"] == "Omkara ARC OMKARA0001 HDFC0004989"
    assert row["downloads_list"][0] == "be-239024-nit.zip" and row["downloads_complete"] is False
    assert row["media"] == [] and row["has_photos"] is False       # the captcha is not a photo
    assert row["auction_status"] == "live"


def test_row_without_detail_still_normalizes():
    ad = BankeauctionsAdapter(state="Tamil Nadu")
    row = ad.normalize({"row": ROW, "detail_url": DETAIL_URL, "detail_html": None}).to_row()
    assert row["reserve_price_num"] == 11700000.0
    assert row["auction_start_dt"] == "2026-09-15T00:00:00"        # date only on the row
    assert row["documents"] == []


def test_movable_rows_are_dropped():
    ad = BankeauctionsAdapter(state="Tamil Nadu")
    vehicle = [*ROW[:12], "Movable", "Vehicle", "0"]
    assert ad.normalize({"row": vehicle, "detail_url": "", "detail_html": None}) is None


def test_expand_bundle_routes_members_by_name(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in ("Account Teezle Telematics Tender Document.pdf", "Omkara-Dinakaran-Chennai-23-08-2026.pdf",
                     "Property Details - TEEZLE TELEMATICS.pdf", "Sale Proclamation dated 23.08.2026.pdf"):
            z.writestr(name, b"%PDF-1.4")
    zpath = tmp_path / "be-239024-nit.zip"
    zpath.write_bytes(buf.getvalue())

    ad = BankeauctionsAdapter(state="Tamil Nadu")
    ref = documents_from_detail(DETAIL_HTML, DETAIL_URL, "239024")[0]
    members = ad.expand_bundle(ref, zpath, tmp_path)
    assert [(m.filename, m.doc_role) for m in members] == [
        ("be-239024-account-teezle-telematics-tender-document.pdf", "tender"),
        ("be-239024-omkara-dinakaran-chennai-23-08-2026.pdf", "publication"),
        ("be-239024-property-details-teezle-telematics.pdf", "property_details"),
        ("be-239024-sale-proclamation-dated-23-08-2026.pdf", "proclamation"),
    ]
    assert all((tmp_path / m.filename).exists() for m in members)


@pytest.mark.parametrize("raw, want", [
    ("Property Details - TEEZLE TELEMATICS.pdf", "property-details-teezle-telematics"),
    ("Annexure 2/Details of Bidders", "annexure-2-details-of-bidders"),
    ("   ", "document"),
])
def test_file_slug(raw, want):
    assert file_slug(raw) == want


# ── harvest, with the network faked ─────────────────────────────────────────

HOMEPAGE = ('<select id="bank_id"><option value="134">The Tamil Nadu Industrial Investment Corporation Limited</option></select>'
            '<select id="state"><option value="">Select</option><option value="24">Tamil Nadu</option><option value="25">Telangana</option></select>')


def make_session(calls, pages):
    class R:
        def __init__(self, text=None, payload=None, status=200):
            self.text, self._payload, self.status_code = text, payload, status

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

    def get(url, **kw):
        calls.append(("GET", url, kw))
        if url.rstrip("/") == "https://bankeauctions.com":
            return R(text=HOMEPAGE)
        return R(text=DETAIL_HTML)

    def post(url, params=None, data=None, **kw):
        calls.append(("POST", url, params, data))
        return R(payload=pages[data["iDisplayStart"]])

    return types.SimpleNamespace(get=get, post=post)


def _row(rid, cat="Immovable"):
    return [*ROW[:1], str(rid), *ROW[2:10], str(rid), "0", cat, "Land", "0"]


def test_harvest_reads_the_state_id_off_the_homepage_and_dedupes_the_overlap():
    # iTotalRecords over-counts on the live site (195 reported, 160 reachable)
    # and pages are pinned to 10; page 2 opens with page 1's last row.
    pages = {
        0: {"iTotalRecords": "12", "aaData": [_row(1), *[_row(2)] * 9]},
        10: {"iTotalRecords": "12", "aaData": [_row(2), _row(3)]},
    }
    calls = []
    ad = BankeauctionsAdapter(state="Tamil Nadu", session=make_session(calls, pages))
    got = list(ad.harvest())

    assert ad.state_id() == 24
    assert [g["row"][1] for g in got] == ["1", "2", "3"]
    assert got[0]["detail_url"] == "https://bankeauctions.com/immovable-land-chennai-1"
    assert got[0]["detail_html"] == DETAIL_HTML
    first_post = next(c for c in calls if c[0] == "POST")
    assert first_post[2] == {"state": 24} and first_post[3]["iDisplayLength"] == 10


def test_harvest_limit_and_no_detail():
    pages = {0: {"iTotalRecords": "2", "aaData": [_row(1), _row(2)]}}
    calls = []
    ad = BankeauctionsAdapter(state="Tamil Nadu", session=make_session(calls, pages), with_detail=False)
    got = list(ad.harvest(limit=1))
    assert [g["row"][1] for g in got] == ["1"] and got[0]["detail_html"] is None
    assert not any(c[0] == "GET" and "immovable" in c[1] for c in calls)


def test_unknown_state_is_an_error():
    ad = BankeauctionsAdapter(state="Atlantis", session=make_session([], {}))
    with pytest.raises(LookupError):
        ad.state_id()
