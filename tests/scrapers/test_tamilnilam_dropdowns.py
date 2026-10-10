"""The walk over a fake listing API: every level lands on disk, a re-run
fetches nothing it already has, and a failed call never looks like an empty
list. The fake answers in the exact shape the GI Viewer's listing API uses
(``{"success": 1, "data": [...]}`` with ``*_english_name`` fields) so the
normaliser is exercised too.
"""
from __future__ import annotations

import csv
import json
from collections import Counter

import pytest

from scrapers.tamilnilam_dropdowns import (ApiError, Store, TamilNilamClient,
                                           export_villages_csv, walk)

DISTRICTS = [
    {"district_code": "01", "district_english_name": "Tiruvallur", "district_tamil_name": "திருவள்ளூர்"},
    {"district_code": "02", "district_english_name": "Chennai"},
]
TALUKS = {
    "01": [{"taluk_code": "03", "taluk_english_name": "Ponneri"}, {"taluk_code": "07", "taluk_english_name": "Avadi"}],
    "02": [{"taluk_code": "01", "taluk_english_name": "Perambur"}],
}
VILLAGES = {
    ("01", "03"): [{"village_code": "012", "village_english_name": "Kadirvedu"},
                   {"village_code": "045", "village_english_name": "Jambuli"}],
    ("01", "07"): [{"village_code": "003", "village_english_name": "Paruthipattu"}],
    ("02", "01"): [],
}
SURVEYS = {
    ("01", "03", "012"): [{"survey_number": "1"}, {"survey_number": "2"}, {"survey_number": "72"}],
    ("01", "03", "045"): [{"survey_number": "5"}],
    ("01", "07", "003"): [],
}


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Answers the four listing paths from the tables above; records every
    call so tests can assert what was (not) fetched."""

    def __init__(self, broken=None):
        self.calls = []
        self.broken = broken or {}  # path -> FakeResponse to return instead
        self.delay_s = 1.0

    def get(self, url, params=None, headers=None, **kw):
        path = url.rsplit("/", 1)[1]
        self.calls.append((path, dict(params or {})))
        assert headers["x-app-name"] == "demo" and headers["x-requested-with"] == "XMLHttpRequest"
        if path in self.broken:
            return self.broken[path]
        p = params
        if path == "admin_master_district":
            data = DISTRICTS
        elif path == "admin_master_taluk":
            data = TALUKS[p["district_code"]]
        elif path == "admin_master_village":
            data = VILLAGES[(p["district_code"], p["taluk_code"])]
        elif path == "admin_master_survey_number":
            assert p["area_type"] == "rural" and p["data_type"] == "cadastral"
            data = SURVEYS[(p["district_code"], p["taluk_code"], p["revenue_village_code"])]
        else:
            raise AssertionError(path)
        return FakeResponse(200, {"success": 1, "data": data})


class FakeSubdivisions:
    def __init__(self):
        self.asked = []

    def fetch(self, dc, tc, vc, sn):
        self.asked.append((dc, tc, vc, sn))
        return ["1A", "1B"] if sn == "1" else []


def _client(session=None, **kw):
    return TamilNilamClient(session or FakeSession(), sleep=lambda s: None, **kw)


def _calls(session, path):
    return [c for c in session.calls if c[0] == path]


def test_walk_writes_every_level_and_resumes(tmp_path):
    s = FakeSession()
    store = Store(tmp_path)
    got = walk(_client(s), store, level="survey", log=lambda m: None)

    assert got == Counter(district=2, taluk=3, village=3, survey=4)
    assert [d["district_name"] for d in store.read_districts()] == ["Tiruvallur", "Chennai"]
    assert store.read_districts()[0]["district_name_ta"] == "திருவள்ளூர்"
    assert {(t["district_code"], t["taluk_name"]) for t in store.rows("taluk")} == {
        ("01", "Ponneri"), ("01", "Avadi"), ("02", "Perambur")}
    kad = store.rows("survey", village_code="012")
    assert sorted(r["survey_number"] for r in kad) == ["1", "2", "72"]
    assert kad[0]["raw"] == {"survey_number": kad[0]["survey_number"]}  # server fields kept verbatim

    # a village the server reports as empty is finished, not retried forever
    assert store.is_done("survey", "01|07|003")

    # second run: districts are re-listed (cheap, and the only way to notice a new one); nothing else is fetched
    s2 = FakeSession()
    got2 = walk(_client(s2), Store(tmp_path), level="survey", log=lambda m: None)
    assert got2 == Counter(district=2)
    assert [c[0] for c in s2.calls] == ["admin_master_district"]


def test_name_filters_limit_what_is_fetched(tmp_path):
    s = FakeSession()
    got = walk(_client(s), Store(tmp_path), level="survey", district="tiruvallur", taluk="ponn",
               village="jamb", log=lambda m: None)
    # filters narrow the walk, but every taluk/village list on the way is still saved whole
    assert got == Counter(district=2, taluk=2, village=2, survey=1)
    assert _calls(s, "admin_master_taluk") == [("admin_master_taluk", {"district_code": "01", "request_type": "taluk"})]
    assert [c[1]["revenue_village_code"] for c in _calls(s, "admin_master_survey_number")] == ["045"]


def test_failed_call_raises_and_marks_nothing_done(tmp_path):
    s = FakeSession(broken={"admin_master_village": FakeResponse(200, {"success": 0, "message": "Invalid request"})})
    store = Store(tmp_path)
    with pytest.raises(ApiError, match="admin_master_village"):
        walk(_client(s, retries=3), store, level="village", log=lambda m: None)
    assert len(_calls(s, "admin_master_village")) == 3  # retried, then gave up
    assert store.rows("village") == []
    assert not store.is_done("village", "01|03")
    assert store.is_done("taluk", "01")  # what succeeded before the failure stays done

    # a non-JSON 500 is the same story
    s = FakeSession(broken={"admin_master_taluk": FakeResponse(502, None, "<html>bad gateway</html>")})
    with pytest.raises(ApiError, match="HTTP 502"):
        walk(_client(s, retries=2), Store(tmp_path / "b"), level="taluk", log=lambda m: None)


def test_subdivision_level_uses_fetcher_and_resumes_per_survey(tmp_path):
    store = Store(tmp_path)
    subs = FakeSubdivisions()
    got = walk(_client(), store, level="subdivision", district="tiruvallur", taluk="ponneri",
               subdivisions=subs, log=lambda m: None)
    assert got["subdivision"] == 2
    assert [(dc, vc, sn) for dc, _, vc, sn in subs.asked] == [("01", "012", "1"), ("01", "012", "2"),
                                                              ("01", "012", "72"), ("01", "045", "5")]
    rows = store.rows("subdivision")
    assert sorted(r["subdivision"] for r in rows) == ["1A", "1B"]
    assert all(r["survey_number"] == "1" and r["area_type"] == "rural" for r in rows)
    # a survey number with no sub-divisions is still finished
    assert store.is_done("subdivision", "01|03|012|72")

    subs2 = FakeSubdivisions()
    walk(_client(), Store(tmp_path), level="subdivision", district="tiruvallur", taluk="ponneri",
         subdivisions=subs2, log=lambda m: None)
    assert subs2.asked == []

    with pytest.raises(ValueError, match="SubdivisionFetcher"):
        walk(_client(), Store(tmp_path / "x"), level="subdivision")


def test_store_rows_dedupes_an_interrupted_append(tmp_path):
    store = Store(tmp_path)
    row = {"district_code": "01", "taluk_code": "03", "village_code": "012", "village_name": "Kadirvedu", "raw": {}}
    store.append("village", [row])
    store.append("village", [row])  # crash between append and mark_done, then re-fetch
    assert len(store.rows("village")) == 1


def test_export_villages_csv_for_the_gazetteer_refresh(tmp_path):
    store = Store(tmp_path)
    walk(_client(), store, level="village", log=lambda m: None)
    out = tmp_path / "tn_villages.csv"
    assert export_villages_csv(store, out) == 3
    with open(out, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["district"] == "Tiruvallur" and rows[0]["taluk"] == "Avadi" and rows[0]["village"] == "Paruthipattu"
    assert {r["village"] for r in rows} == {"Kadirvedu", "Jambuli", "Paruthipattu"}
    # the TNGIS codes travel under headers the refresh script does not map onto its own village_code
    assert set(rows[0]) == {"district", "taluk", "village", "name_ta",
                            "tngis_district_code", "tngis_taluk_code", "tngis_village_code"}
    assert rows[0]["tngis_village_code"] == "003"


def test_client_sends_cookie_and_app_name_when_given():
    s = FakeSession()
    c = TamilNilamClient(s, app_name="demo", cookie="sid=abc", sleep=lambda x: None)
    assert c.headers["Cookie"] == "sid=abc"
    c.districts()
    assert s.calls == [("admin_master_district", {"request_type": "district"})]
