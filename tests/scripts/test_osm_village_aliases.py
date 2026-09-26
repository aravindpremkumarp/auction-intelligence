"""scripts/harvest_osm_village_aliases (what OSM evidence counts) and the
resolver hook that applies it (scripts/resolve_places.apply_osm_alias).

Result shapes are the Nominatim jsonv2 rows the pilot run returned."""
from __future__ import annotations

from pipeline.place_resolution import Gazetteer, resolve_place
from pipeline.resolution_review import village_alias_key
from scripts.harvest_osm_village_aliases import judge
from scripts.resolve_places import apply_osm_alias


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
