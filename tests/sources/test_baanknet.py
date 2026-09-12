"""BAANKNET adapter on the shapes recorded live on 2026-09-12.

``SEARCH_ROW`` is a real ``_source`` from ``POST property-filter`` (Indian
Overseas Bank, Tirunelveli, auction 359826); ``DETAIL`` is a real
``GET auction/detail`` record (Bank of Maharashtra, Chakan, auction 297346),
trimmed to the keys the adapter reads. They are two different auctions on
purpose — the tests pair them to prove which side each field is read from.
"""
from __future__ import annotations

import json
import types

import pytest

from sources.baanknet import BaanknetAdapter, asset_category_for, auction_type_for, property_type_for

SEARCH_ROW = {
    "propertyTypeId": 1, "propertyType": "Residential",
    "propertyTitle": "D no 81A BY 1 81A BY 2, S.No.381 BY 5A, ward no 11, chatthiram kudiyirupu, naranammalpuram village, tirunelveli, total extent 1215 sqft",
    "propertyDetailId": 119851, "propertySubTypeId": 2, "propertySubType": "Individual House",
    "propertyPossessionTypeId": 2, "propertyPossessionType": "Symbolic",
    "propertyOwnershipType": "Freehold", "propertyUniqueId": "IOBA138800001", "propertyPrice": 4560000,
    "pincode": "627357", "cityId": None, "cityName": None, "districtId": 604, "districtName": "Tirunelveli",
    "stateId": 31, "stateName": "Tamil Nadu", "departmentName": "NARANAMMALPURAM-1388",
    "bankId": 7, "bankName": "Indian Overseas Bank",
    "locality": None, "propTypeOfAction": None,
    "ownerName": "N MARIAPPAN", "borrowerName": "N MARIAPPAN", "borrowerAddress": None,
    "area": 0, "builtUpArea": 0, "measurementAbb": None,
    "auctionId": 359826, "isAuctionAvailable": True,
    "auctionEndTime": "2099-09-24T07:30:00.000Z", "auctionStartTime": "2099-09-24T05:30:00.000Z",
    "auctionPrice": "4626500.00000",
    "emdStartTime": "2026-09-11T05:54:14.000Z", "emdEndTime": "2026-09-23T11:35:00.000Z",
    "auctionStatus": 1,
    "inspectionStart": "2026-09-11T09:35:00.000Z", "inspectionEnd": "2026-09-23T11:30:00.000Z",
    "inspectionName": "ASWATHY V S", "inspectionMobileNo": "9746484418",
    "propertyHeading": "Individual House for sale in",
    "photos": [
        "https://cdn.baanknet.com/Production/Application-Documents/Generic-Instance/Property/Images/2024/07/03/119851/119740.jpg",
        "https://cdn.baanknet.com/Production/Application-Documents/Generic-Instance/Property/Images/2026/09/11/119851/648449.jpeg",
    ],
}

DETAIL = {
    "auctionId": 297346,
    "auctionFrom": "2026-05-14T05:30:00.000Z", "auctionTo": "2026-05-14T08:30:00.000Z",
    "emd": "513000.00000", "emdStart": "2026-05-11T07:07:44.000Z", "emdEnd": "2026-05-13T17:30:00.000Z",
    "reservePrice": 5130000, "incrementPrice": 100000, "extendBy": 10, "extendTime": 10,
    "inspectionStart": "2026-05-11T07:30:00.000Z", "inspectionEnd": "2026-05-13T11:30:00.000Z",
    "inspectionName": "SUNIL SAHU", "inspectionMobileNo": "9420753667",
    "auctionStatus": 1, "propertyDetailId": 280354, "propertyUniqueId": "MAHB202608",
    "pincode": "410501", "cityName": "Chakan", "stateName": "Maharashtra", "districtName": "Pune", "locality": None,
    "address": "Nagar Parishad Property No. 5, building name \"Parashram Smruti\" situated and lying and being at Village Chakan",
    "propertyPossessionType": "Symbolic", "propertySubType": "Shops", "propertyType": "Commercial",
    "typeOfAction": "Under SARFAESI",
    "borrowerName": "M/s Vishal Tyres", "borrowerAddress": "Pardeshi Building, Market Yard, Chakan, Pune",
    "propertyBankName": "Bank of Maharashtra", "propertyBranchName": "ASSETRECOVERYBRANCHPUNE-1453", "auctionBranch": "CHAKAN",
    "carpetAreaSqft": "600.00", "builtupAreaSqft": None, "measurementAbb": "sq feet",
    "auctionDocuments": [
        {"size": 1654041, "filename": "SALE NOTICE.pdf", "description": "SALE NOTICE",
         "url": "https://cdn.baanknet.com/Production/Application-Documents/Generic-Instance/Auction/Auctioneer/auction-document/44/297346/379330.pdf"},
        {"size": 2365609, "filename": "TERMS AND CONDITIONS OF SALE.pdf", "description": "TERMS AND CONDITIONS OF SALE",
         "url": "https://cdn.baanknet.com/Production/Application-Documents/Generic-Instance/Auction/Auctioneer/auction-document/44/297346/379332.pdf"},
    ],
    "propertyMedia": [
        {"size": 112027, "filename": "photo_Vishal tyres.jpg", "filetype": 1, "ismainimage": "base64:type16:AQ==",
         "url": "https://cdn.baanknet.com/Production/Application-Documents/Generic-Instance/Property/Images/2026/03/13/280354/561335.jpg"},
        {"size": 133809, "filename": "IMG_0155.jpeg", "filetype": 1, "ismainimage": "base64:type16:AA==",
         "url": "https://cdn.baanknet.com/Production/Application-Documents/Generic-Instance/Property/Images/2026/09/09/297502/646923.jpeg"},
        {"size": 2131085, "filename": "walkthrough.mp4", "filetype": 2, "ismainimage": "base64:type16:AA==",
         "url": "https://cdn.baanknet.com/Production/Application-Documents/Generic-Instance/Property/Images/2026/09/10/297776/648366.mp4"},
    ],
}


@pytest.fixture
def tn():
    return BaanknetAdapter(state="Tamil Nadu")


def test_search_row_alone_is_a_complete_listing(tn):
    row = tn.normalize({"row": SEARCH_ROW, "detail": None}).to_row()

    assert row["auction_id"] == "bn-359826" and row["source_id"] == "359826"
    assert row["source"] == "baanknet" and row["source_rank"] == 1
    assert row["url"] == "https://baanknet.com/property-detail/119851"
    assert row["bank_name"] == "Indian Overseas Bank" and row["branch_name"] == "NARANAMMALPURAM-1388"
    assert row["borrower_name"] == "N MARIAPPAN"
    assert row["reserve_price_num"] == 4626500.0            # auctionPrice, not propertyPrice
    assert row["auction_start_dt"] == "2099-09-24T11:00:00"  # UTC → IST
    assert row["application_deadline_dt"] == "2026-09-23T17:05:00"
    assert row["district"] == "Tirunelveli" and row["state"] == "Tamil Nadu" and row["pincode"] == "627357"
    assert row["city"] == ""                                   # cityName null on this row
    assert row["asset_category"] == "Residential"
    assert row["property_types"] == ["House"] and row["property_type_raw"] == "Residential / Individual House"
    assert row["possession_type"] == "symbolic"
    assert row["auction_status"] == "live"
    assert row["inspection_start_dt"] == "2026-09-11T15:05:00"
    assert row["contact_details"] == "ASWATHY V S 9746484418"
    assert row["service_provider"] == "BAANKNET"
    # no detail → no documents yet, photos from the row, first one main
    assert row["documents"] == [] and row["downloads_complete"] is True
    assert [m["kind"] for m in row["media"]] == ["image", "image"]
    assert [m["is_main"] for m in row["media"]] == [True, False]
    assert row["has_photos"] is True


def test_detail_supplies_documents_media_and_the_bid_facts():
    ad = BaanknetAdapter(state="Maharashtra")
    row = ad.normalize({"row": {**SEARCH_ROW, "stateName": "Maharashtra", "auctionId": 297346}, "detail": DETAIL}).to_row()

    assert row["auction_id"] == "bn-297346"
    assert row["reserve_price_num"] == 5130000.0 and row["emd_num"] == 513000.0
    assert row["bid_increment_num"] == 100000.0
    assert row["borrower_address"] == "Pardeshi Building, Market Yard, Chakan, Pune"
    assert row["auction_type"] == "SARFAESI Auction"
    assert row["extent_raw"] == "carpet 600.00 sq feet"
    # documents: unique filenames from the CDN basename, roles from the label
    assert [(d["filename"], d["doc_role"], d["label"]) for d in row["documents"]] == [
        ("bn-379330.pdf", "sale_notice", "SALE NOTICE"),
        ("bn-379332.pdf", "terms", "TERMS AND CONDITIONS OF SALE"),
    ]
    assert row["downloads_list"] == ["bn-379330.pdf", "bn-379332.pdf"]
    assert row["downloads_missing"] == row["downloads_list"] and row["downloads_complete"] is False
    # media: the API's filetype enum and the BIT-encoded main flag
    assert [(m["kind"], m["is_main"]) for m in row["media"]] == [("image", True), ("image", False), ("video", False)]
    assert row["has_photos"] is True


def test_a_property_without_an_auction_is_not_a_listing(tn):
    assert tn.normalize({"row": {**SEARCH_ROW, "auctionId": None, "isAuctionAvailable": False}, "detail": None}) is None


def test_other_states_are_dropped(tn):
    assert tn.normalize({"row": {**SEARCH_ROW, "stateName": "Tripura"}, "detail": None}) is None


def test_ended_auction_status(tn):
    row = tn.normalize({"row": {**SEARCH_ROW, "auctionEndTime": "2020-01-01T05:30:00.000Z"}, "detail": None}).to_row()
    assert row["auction_status"] == "ended"


def test_bare_source_dict_is_accepted_too(tn):
    assert tn.normalize(SEARCH_ROW).to_row()["auction_id"] == "bn-359826"


@pytest.mark.parametrize("raw, want", [
    ("Residential", "Residential"), ("Commercial", "Commercial"), ("Industrial", "Industrials"),
    ("Agriculture", "Residential"),   # the graph files agricultural land under Residential
    ("Other", ""), (None, ""), ("Hospitality", "Hospitality"),   # unmapped passes through
])
def test_asset_category_for(raw, want):
    assert asset_category_for("baanknet", raw) == want


@pytest.mark.parametrize("raw, want", [
    ("Individual House", "House"), ("Flat", "Flat"), ("Shops", "Commercial Shop"),
    ("Factory", "Factory land and Building"), ("Agricultural Land", "Agricultural Land"),
    ("Penthouse", "Penthouse"),
])
def test_property_type_for(raw, want):
    assert property_type_for("baanknet", raw) == want


@pytest.mark.parametrize("raw, want", [
    ("Under SARFAESI", "SARFAESI Auction"), ("DRT", "DRT Auction"), ("Under IBC", "Liquidation Auction"),
    (None, ""), ("Something else", "Something else"),
])
def test_auction_type_for(raw, want):
    assert auction_type_for(raw) == want


# ── harvest, with the network faked ─────────────────────────────────────────

class FakeResp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def make_session(calls):
    def get(url, params=None, **kw):
        calls.append(("GET", url, params))
        if url.endswith("/common/states"):
            return FakeResp({"data": [{"id": 21, "name": "Maharashtra", "code": "27"},
                                      {"id": 31, "name": "Tamil Nadu", "code": "33"},
                                      {"id": 33, "name": "Tripura", "code": "16"}]})
        if "/auction/detail/" in url:
            aid = int(url.rsplit("/", 1)[-1])
            return FakeResp({"data": {**DETAIL, "auctionId": aid}}) if aid != 404 else FakeResp({}, 404)
        raise AssertionError(url)

    def post(url, json=None, **kw):
        calls.append(("POST", url, json))
        page = json["page"]
        hits = {
            1: [{"_id": "1", "_source": {**SEARCH_ROW, "propertyDetailId": 1, "auctionId": 11}},
                {"_id": "2", "_source": {**SEARCH_ROW, "propertyDetailId": 2, "auctionId": None, "isAuctionAvailable": False}}],
            2: [{"_id": "2", "_source": {**SEARCH_ROW, "propertyDetailId": 2, "auctionId": None}},   # overlap
                {"_id": "3", "_source": {**SEARCH_ROW, "propertyDetailId": 3, "auctionId": 404}}],
        }[page]
        return FakeResp({"data": {"data": hits, "total": 3, "totalPages": 2, "currentPage": page}})

    return types.SimpleNamespace(get=get, post=post)


def test_harvest_resolves_the_state_by_name_pages_and_dedupes():
    calls = []
    ad = BaanknetAdapter(state="Tamil Nadu", session=make_session(calls), page_size=2)
    got = list(ad.harvest())

    assert ad.state_id() == 31                               # id, not the "code" 33
    assert [g["row"]["propertyDetailId"] for g in got] == [1, 2, 3]
    assert got[0]["detail"]["auctionId"] == 11
    assert got[1]["detail"] is None                          # no auction → no detail call
    assert got[2]["detail"] is None                          # 404 on the detail is tolerated
    posted = [c for c in calls if c[0] == "POST"]
    assert posted[0][2]["search"] == {"stateId": 31} and posted[0][2]["limit"] == 2
    assert len([c for c in calls if "/common/states" in c[1]]) == 1   # cached


def test_harvest_limit_and_live_only():
    calls = []
    ad = BaanknetAdapter(state="Tamil Nadu", session=make_session(calls), page_size=2, live_only=True, with_detail=False)
    got = list(ad.harvest(limit=1))
    assert [g["row"]["propertyDetailId"] for g in got] == [1]
    assert not any("/auction/detail/" in c[1] for c in calls)


def test_unknown_state_is_an_error():
    ad = BaanknetAdapter(state="Atlantis", session=make_session([]))
    with pytest.raises(LookupError):
        ad.state_id()


def test_to_row_is_json_serialisable(tn):
    json.dumps(tn.normalize({"row": SEARCH_ROW, "detail": DETAIL}).to_row())
