"""The TNREGINET walk over a fake portal shaped like the real one (mapped
2026-10-12): loose <option> tags, the village reply's trailing "&<district>",
an HTML result table, and a _csrf meta tag."""
from __future__ import annotations

import json

import pytest

from scrapers.tnreginet_guideline import (PortalError, Store, TnreginetClient, parse_options,
                                          parse_streets, walk)

PAGE = """<html><head><meta name="_csrf" content="tok-1"/></head><body>
<select id="cmb_Zone"><option value='-1'>- Select -</option>
<option value='2'>Chennai</option><option value='5'>Madurai</option></select></body></html>"""
SROS = {"2": "<option value='-1'>- Select -</option><option value='20051'>Adayar</option>"
             "<option value='20052'>Alandur</option>"}
VILLAGES = {"20051": "<option value='-1'>- Select -</option><option value='1'>Adayar</option>"
                     "<option value='9'>Venkatapuram</option>&Chennai",
            "20052": "<option value='-1'>- Select -</option>&Chennai"}
TABLE = """<table><tr><th>Sr.No</th><th>Street Name</th><th>Guideline Value (Rs./Square Feet)</th>
<th>Guideline Value (Rs./Square Metre)</th><th>Land Classification</th><th>Effective Start Date</th>
<th>G.O</th></tr>{rows}</table>"""
STREETS = {"a": ["ALANDUR ROAD", "GANDHI NAGAR"], "e": ["ALANDUR ROAD", "FIRST STREET"]}


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
        return Resp(PAGE)

    def post(self, url, params=None, data=None, **kw):
        assert data["_csrf"] == "tok-1"
        action = params["actionVal"]
        self.calls.append((action, params, data))
        if action == "getSroOptionByDistrict":
            return Resp(SROS[params["districtId"]])
        if action == "getVillageOptionBySro":
            return Resp(VILLAGES[params["sroId"]])
        if action == "criteriaSearch":
            names = STREETS.get(data["txt_SearchCriteria"], [])
            return Resp(TABLE.format(rows="".join(_row(n) for n in names)))
        raise AssertionError(action)


def _client(session=None):
    return TnreginetClient(session=session or FakeSession(), delay=0)


def test_options_drop_the_placeholder_and_the_trailing_district():
    assert parse_options(VILLAGES["20051"]) == [("1", "Adayar"), ("9", "Venkatapuram")]
    assert parse_options(VILLAGES["20052"]) == []


def test_street_table_is_read_by_its_header():
    rows = parse_streets(TABLE.format(rows=_row("ALANDUR ROAD")))
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


def test_streets_are_the_union_of_the_search_terms(tmp_path):
    store = Store(tmp_path)
    walk(_client(), store, level="street", sro="Adayar", village="Adayar",
         terms=("a", "e"), log=lambda *_: None)
    assert sorted(r["street"] for r in store.read("streets.jsonl")) == [
        "ALANDUR ROAD", "FIRST STREET", "GANDHI NAGAR"]


def test_a_rerun_fetches_nothing_already_done(tmp_path):
    walk(_client(), Store(tmp_path), level="street", log=lambda *_: None)
    again = FakeSession()
    walk(_client(again), Store(tmp_path), level="street", log=lambda *_: None)
    assert [c[0] for c in again.calls] == ["getSroOptionByDistrict"]


def test_the_search_sends_names_and_the_village_id():
    s = FakeSession()
    _client(s).streets("Chennai", "Adayar", "Adayar", "1", "a")
    _, params, data = s.calls[-1]
    assert params["_csrf"] == "tok-1" and data["cmb_reg_village"] == "1"
    assert (data["zoneName"], data["sroName"], data["villageName"]) == ("Chennai", "Adayar", "Adayar")


def test_a_failed_call_raises_instead_of_recording_nothing(tmp_path):
    class Broken(FakeSession):
        def post(self, url, params=None, data=None, **kw):
            return Resp("", 500)
    with pytest.raises(PortalError):
        walk(_client(Broken()), Store(tmp_path), level="village", log=lambda *_: None)
    assert Store(tmp_path).read("villages.jsonl") == []


def test_a_paginated_reply_is_flagged():
    class Paged(FakeSession):
        def post(self, url, params=None, data=None, **kw):
            return Resp(TABLE.format(rows=_row("X")) + "<a class='paginate_button'>Next</a>")
    rows, paged = _client(Paged()).streets("Chennai", "Adayar", "Adayar", "1", "a")
    assert rows and paged
