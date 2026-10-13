"""The TNREGINET walk over a fake portal shaped like the real one (mapped
2026-10-12): a Tamil first page switched to English, loose <option> tags,
the village reply's trailing "&<district>", a _csrf meta tag, and a street
list paged 10 rows at a time under an "N items found" banner."""
from __future__ import annotations

import json

import pytest

from scrapers.tnreginet_guideline import (PortalError, Store, TnreginetClient, parse_options,
                                          parse_streets, parse_total, walk)

PAGE = """<html><head><meta name="_csrf" content="tok-1"/></head><body>
<select id="cmb_Zone"><option value='-1'>- Select -</option>
<option value='2'>Chennai</option><option value='5'>Madurai</option></select></body></html>"""
SROS = {"2": "<option value='-1'>- Select -</option><option value='20051'>Adayar</option>"
             "<option value='20052'>Alandur</option>"}
VILLAGES = {"20051": "<option value='-1'>- Select -</option><option value='1'>Adayar</option>"
                     "<option value='9'>Venkatapuram</option>&Chennai",
            "20052": "<option value='-1'>- Select -</option>&Chennai"}
TAMIL_PAGE = """<html><head><meta name="_csrf" content="tok-0"/></head><body>
<select id="districtList"><option value='2'>சென்னை</option></select></body></html>"""
CRITERIA = """<table><tr><td>Zone:</td><td><b>CHENNAI</b></td></tr></table>"""
TABLE = """<table class="its" id="surveyTable"><thead><tr><th>Sr.No.</th><th>Street Name</th>
<th>Guideline Value (&#8377) (British Value)</th><th>Guideline Value (&#8377) (Metric Value)</th>
<th>Land Classification</th><th>Effective Start Date</th><th>G.O.Download</th></tr></thead>
<tbody>{rows}</tbody></table>"""
STREETS = {"1": ["ALANDUR ROAD", "FIRST STREET", "GANDHI NAGAR"], "9": []}
PER_PAGE = 2


def _row(name):
    return (f"<tr><td>1</td><td>{name}</td><td>6100/Square Feet</td><td>65665/Square Metre</td>"
            f"<td>Residential Class II Type-I</td><td>01-Jul-2024</td><td>x</td></tr>")


class Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kw):
        return Resp(TAMIL_PAGE)

    def post(self, url, params=None, data=None, **kw):
        action = params["actionVal"]
        self.calls.append((action, params, data))
        if action == "homePage":
            assert params["UserLocaleID"] == "en" and data["_csrf"] == "tok-0"
            return Resp(PAGE)
        assert data["_csrf"] == "tok-1"
        if action == "getSroOptionByDistrict":
            return Resp(SROS[params["districtId"]])
        if action == "getVillageOptionBySro":
            return Resp(VILLAGES[params["sroId"]])
        if action == "listAllStreet":
            return Resp(_street_page(STREETS[data["villageId"]], int(params.get("d-9450-p", 1))))
        raise AssertionError(action)


def _street_page(names, page):
    if not names:
        return CRITERIA + '<span class="pagebanner">No items found.</span>'
    chunk = names[(page - 1) * PER_PAGE:page * PER_PAGE]
    links = '<span class="pagelinks"><a href="/portal/webHP?a=1&amp;d-9450-p=2">2</a></span>'
    return (CRITERIA + f'<span class="pagebanner">{len(names)} items found, displaying x.</span>'
            + links + TABLE.format(rows="".join(_row(n) for n in chunk)))


def _client(session=None):
    return TnreginetClient(session=session or FakeSession(), delay=0)


def test_options_drop_the_placeholder_and_the_trailing_district():
    assert parse_options(VILLAGES["20051"]) == [("1", "Adayar"), ("9", "Venkatapuram")]
    assert parse_options(VILLAGES["20052"]) == []


def test_street_table_is_read_by_its_header():
    rows = parse_streets(CRITERIA + TABLE.format(rows=_row("ALANDUR ROAD")))
    assert rows == [{"street": "ALANDUR ROAD", "value_sqft": "6100/Square Feet",
                     "value_sqm": "65665/Square Metre",
                     "classification": "Residential Class II Type-I",
                     "effective_from": "01-Jul-2024"}]


def test_village_level_writes_sro_village_lists(tmp_path):
    store = Store(tmp_path)
    stats = walk(_client(), store, level="village", log=lambda *_: None)
    rows = store.read("villages.jsonl")
    assert stats["sros"] == 2
    assert [(r["sro"], r["village_id"], r["village"]) for r in rows] == [
        ("Adayar", "1", "Adayar"), ("Adayar", "9", "Venkatapuram")]
    assert json.loads((tmp_path / "districts.json").read_text()) == {"2": "Chennai", "5": "Madurai"}


def test_item_count_banner():
    assert parse_total('<span class="pagebanner">1,096 items found, displaying 1 to 10.</span>') == 1096
    assert parse_total('<span class="pagebanner">One item found.</span>') == 1
    assert parse_total('<span class="pagebanner">No items found.</span>') == 0
    assert parse_total('<div id="nothing" align="center"> Nothing Found to Display </div>') == 0
    assert parse_total("<p>nothing</p>") is None


def test_streets_are_read_across_every_page(tmp_path):
    store = Store(tmp_path)
    stats = walk(_client(), store, level="street", sro="Adayar", log=lambda *_: None)
    assert [r["street"] for r in store.read("streets.jsonl")] == [
        "ALANDUR ROAD", "FIRST STREET", "GANDHI NAGAR"]
    assert stats["streets"] == 3 and stats["empty_villages"] == 1


def test_a_rerun_fetches_nothing_already_done(tmp_path):
    walk(_client(), Store(tmp_path), level="street", log=lambda *_: None)
    again = FakeSession()
    walk(_client(again), Store(tmp_path), level="street", log=lambda *_: None)
    assert [c[0] for c in again.calls] == ["homePage", "getSroOptionByDistrict"]


def test_the_list_sends_names_and_the_village_id():
    s = FakeSession()
    _client(s).streets("Chennai", "Adayar", "Adayar", "1")
    _, params, data = s.calls[-1]
    assert params["_csrf"] == "tok-1" and params["villageIdAllStreet"] == "1"
    assert params["d-9450-p"] == "2" and data["villageId"] == "1"
    assert (data["zoneName"], data["sroName"], data["villageName"]) == ("Chennai", "Adayar", "Adayar")


def test_a_failed_call_raises_instead_of_recording_nothing(tmp_path):
    class Broken(FakeSession):
        def post(self, url, params=None, data=None, **kw):
            return Resp("", 500)
    with pytest.raises(PortalError):
        walk(_client(Broken()), Store(tmp_path), level="village", log=lambda *_: None)
    assert Store(tmp_path).read("villages.jsonl") == []


def test_a_short_street_list_raises():
    class Short(FakeSession):
        def post(self, url, params=None, data=None, **kw):
            if params["actionVal"] != "listAllStreet":
                return super().post(url, params, data, **kw)
            return Resp('<span class="pagebanner">5 items found.</span>' + TABLE.format(rows=_row("X")))
    with pytest.raises(PortalError):
        _client(Short()).streets("Chennai", "Adayar", "Adayar", "1")


def test_a_network_drop_is_retried(monkeypatch):
    import requests
    monkeypatch.setattr("scrapers.tnreginet_guideline.time.sleep", lambda *_: None)

    class Flaky(FakeSession):
        drops = 2

        def post(self, url, params=None, data=None, **kw):
            if params["actionVal"] == "getSroOptionByDistrict" and self.drops:
                self.drops -= 1
                raise requests.ConnectionError("getaddrinfo failed")
            return super().post(url, params, data, **kw)
    assert _client(Flaky()).sros("2") == [("20051", "Adayar"), ("20052", "Alandur")]
