"""scripts/harvest_osm_village_aliases (what OSM evidence counts) and the
step that applies it (pipeline/resolution_review.settle_village), which both
place writers take: listings (scripts/resolve_places) and lots
(pipeline/promote_extractions.lot_place).

Result shapes are the Nominatim jsonv2 rows the pilot run returned."""
from __future__ import annotations

import pipeline.promote_extractions as P
from pipeline.place_resolution import Gazetteer, normalize_place, resolve_place
from pipeline.resolution_review import apply_osm_alias, settle_village, village_alias_key
from scripts.harvest_osm_village_aliases import judge


def _hit(name, ta=None, county="Sholinganallur", kind="village", osm_id=1):
    nd = {"name": name}
    if ta:
        nd["name:ta"] = ta
    return {"osm_type": "node", "osm_id": osm_id, "addresstype": kind,
            "namedetails": nd,
            "address": {"county": county, "state": "Tamil Nadu"}}


REG = [{"name": "Enchambakkam", "name_ta": "ஈஞ்சம்பாக்கம்"},
       {"name": "Kottivakkam", "name_ta": "கொட்டிவாக்கம்"}]
ITEM = {"village": "Injambakkam", "taluk": "Sholinganallur", "district": "Chennai"}


def test_a_matching_tamil_name_in_the_same_taluk_confirms_the_village():
    v = judge(ITEM, [_hit("Injambakkam", "ஈஞ்சம்பாக்கம்")], REG)
    assert v["target"] == "Enchambakkam" and v["rule"] == "osm-tamil"
    assert v["osm"] == "node/1"                       # the evidence travels with it


def test_osm_in_another_taluk_is_no_evidence():
    assert judge(ITEM, [_hit("Injambakkam", "ஈஞ்சம்பாக்கம்", county="Tambaram")],
                 REG) is None


def test_a_road_or_lake_carrying_the_name_is_no_evidence():
    assert judge(ITEM, [_hit("Injambakkam Road", "ஈஞ்சம்பாக்கம்", kind="road")],
                 REG) is None


def test_two_register_villages_close_to_the_spelling_are_refused():
    reg = [{"name": "Veerapandi", "name_ta": "வீரபாண்டி"},
           {"name": "Veerapandi.", "name_ta": "வீரபாண்டிபுதூர்"}]
    item = {"village": "Veerapandi", "taluk": "Coimbatore North"}
    hit = _hit("Veerapandi", "வீரபாண்டி", county="Coimbatore North")
    assert judge(item, [hit], reg) is None


def test_an_urban_place_with_nothing_close_in_the_register_is_urban():
    item = {"village": "Pammal", "taluk": "Pallavaram"}
    reg = [{"name": "Anakaputhur", "name_ta": "அனகாபுத்தூர்"}]
    v = judge(item, [_hit("Pammal", "பம்மல்", county="Pallavaram", kind="suburb")], reg)
    assert v["rule"] == "osm-urban" and v["target"] is None
    # a hamlet belongs to some revenue village: never declared urban
    assert judge(item, [_hit("Pammal", county="Pallavaram", kind="hamlet")], reg) is None


def _gaz():
    return Gazetteer(districts=["Chennai"], taluks=[("Sholinganallur", "Chennai")],
                     villages=[("Enchambakkam", "Sholinganallur", "Chennai")])


def test_the_resolver_applies_an_alias_only_into_a_village_the_register_holds():
    gaz = _gaz()
    res = resolve_place(gaz, district="Chennai", taluk="Sholinganallur",
                        village="Injambakkam")
    assert res["village_status"] == "unmatched"
    osm = {village_alias_key("Injambakkam", "Sholinganallur"):
           {"target": "Enchambakkam", "rule": "osm-tamil"}}
    out = apply_osm_alias(gaz, res, "Injambakkam", osm)
    assert (out["village"], out["village_status"], out["village_source"]) == \
        ("Enchambakkam", "resolved", "osm-tamil")
    stale = {village_alias_key("Injambakkam", "Sholinganallur"):
             {"target": "Nowhere", "rule": "osm-tamil"}}
    assert apply_osm_alias(gaz, res, "Injambakkam", stale) == res


def test_the_resolver_marks_urban_without_placing_and_leaves_other_statuses():
    gaz = _gaz()
    res = resolve_place(gaz, district="Chennai", taluk="Sholinganallur",
                        village="Semmancheri")
    osm = {village_alias_key("Semmancheri", "Sholinganallur"):
           {"target": None, "rule": "osm-urban"}}
    out = apply_osm_alias(gaz, res, "Semmancheri", osm)
    assert out["village"] is None
    assert out["village_status"] == "not-a-revenue-village"
    assert out["village_source"] == "osm-urban"
    done = {**res, "village_status": "resolved", "village": "Enchambakkam"}
    assert apply_osm_alias(gaz, done, "Semmancheri", osm) is done


def test_the_tamil_fold_drops_the_register_code_brackets_and_sandhi():
    from scripts.harvest_osm_village_aliases import fold_ta
    assert fold_ta("071  புஞ்சை புளியம்பட்டி") == fold_ta("புஞ்சைப் புளியம்பட்டி")
    assert fold_ta("ஆத்தூர் (சேலம்)") == fold_ta("ஆத்தூர்")
    # different names stay different
    assert fold_ta("வீரபாண்டி") != fold_ta("வீரபாண்டிபுதூர்")


def test_a_numbered_sub_village_is_never_aliased_onto_the_plain_name():
    reg = [{"name": "Elavur", "name_ta": "எளாவூர்"}]
    item = {"village": "Elavur II", "taluk": "Gummidipoondi"}
    hit = _hit("Elavur", "எளாவூர்", county="Gummidipoondi")
    assert judge(item, [hit], reg) is None
    # the same village with no number is fine
    item2 = {"village": "Elavoor", "taluk": "Gummidipoondi"}
    assert judge(item2, [hit], reg)["target"] == "Elavur"


def test_a_place_the_register_holds_under_another_name_is_never_urban():
    item = {"village": "Mahabalipuram", "taluk": "Tirukalukundram"}
    reg = [{"name": "Mamallapuram", "name_ta": "மாமல்லபுரம்"}]
    hit = _hit("Mahabalipuram", "மகாபலிபுரம்", county="Tirukalukundram", kind="town")
    assert judge(item, [hit], reg) is None


def test_a_human_verdict_outranks_openstreetmap():
    gaz = Gazetteer(districts=["Chennai"], taluks=[("Sholinganallur", "Chennai")],
                    villages=[("Enchambakkam", "Sholinganallur", "Chennai"),
                              ("Kottivakkam", "Sholinganallur", "Chennai")])
    res = resolve_place(gaz, district="Chennai", taluk="Sholinganallur",
                        village="Injambakkam")
    key = village_alias_key("Injambakkam", "Sholinganallur")
    osm = {key: {"target": "Enchambakkam", "rule": "osm-tamil"}}
    out = settle_village(gaz, res, "Injambakkam", aliases={key: "Kottivakkam"},
                         skips=set(), osm=osm)
    assert (out["village"], out["village_source"]) == ("Kottivakkam", "human-alias")
    skipped = settle_village(gaz, res, "Injambakkam", aliases={},
                             skips={normalize_place("Injambakkam")}, osm=osm)
    assert (skipped["village"], skipped["village_status"]) == \
        (None, "not-a-revenue-village")
    # no verdict at all: OSM decides
    assert settle_village(gaz, res, "Injambakkam", aliases={}, skips=set(),
                          osm=osm)["village_source"] == "osm-tamil"


def test_a_lot_takes_the_same_decided_spellings_as_its_listing(monkeypatch):
    osm = {village_alias_key("Injambakkam", "Sholinganallur"):
           {"target": "Enchambakkam", "rule": "osm-tamil"},
           village_alias_key("Semmancheri", "Sholinganallur"):
           {"target": None, "rule": "osm-urban"}}
    monkeypatch.setattr(P, "gazetteer", _gaz)
    monkeypatch.setattr(P, "decided_spellings", lambda: ({}, set(), osm))

    def lot(village):
        return P.lot_place({"lot_key": "n#1", "location": {
            "district": "Chennai", "taluk": "Sholinganallur", "village": village}})

    placed = lot("Injambakkam")
    assert (placed["village"], placed["taluk"], placed["status"], placed["source"]) == \
        ("Enchambakkam", "Sholinganallur", "resolved", "osm-tamil")
    urban = lot("Semmancheri")
    assert (urban["village"], urban["status"]) == (None, "not-a-revenue-village")
    assert lot("Enchambakkam")["source"] == "taluk"      # a plain match is untouched
