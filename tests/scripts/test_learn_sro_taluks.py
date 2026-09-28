"""scripts/learn_sro_taluks: which taluk each sub-registrar office points to,
and the SRO hint reaching the listing resolver."""
from __future__ import annotations

from pipeline.place_resolution import Gazetteer, sro_key
from scripts.learn_sro_taluks import LEARN_FROM, MIN_LOTS, learn, learn_pins


def test_an_office_names_the_taluk_nearly_all_its_lots_sit_in():
    pairs = ([("Chengalpet Joint-II SRO", "Chengalpattu")] * 5
             + [("Chengalpet", "Chengalpattu")] * 5)
    row = learn(pairs)[sro_key("Chengalpet")]
    assert (row["taluk"], row["lots"], row["share"]) == ("Chengalpattu", 10, 1.0)
    # the spellings that folded together, most quoted first
    assert row["spellings"] == ["Chengalpet Joint-II SRO", "Chengalpet"]


def test_an_office_spread_across_taluks_names_none():
    # the office named Chidambaram serves land outside Chidambaram taluk
    pairs = [("Chidambaram", "Chidambaram")] * 8 + [("Chidambaram", "Bhuvanagiri")] * 2
    assert learn(pairs) == {}
    assert learn(pairs + [("Chidambaram", "Chidambaram")] * 10)  # 18 of 20 is enough


def test_too_few_lots_teach_nothing():
    assert learn([("Uthangarai", "Uthangarai")] * (MIN_LOTS - 1)) == {}
    assert learn([("SRO", "Uthangarai")] * 9 + [("Uthangarai", None)] * 9) == {}


def test_the_table_never_learns_from_its_own_answers():
    assert not LEARN_FROM & {"sro-taluk", "city-taluk", "pin-taluk", "district-sound"}


def test_a_pin_names_the_taluk_nearly_all_its_lots_sit_in():
    pairs = [("600017", "Guindy")] * 11 + [("600017", "Mambalam")]
    assert learn_pins(pairs) == {"600017": {"taluk": "Guindy", "lots": 12, "share": 0.917}}
    # a PIN that spans taluks names none
    assert learn_pins([("603103", "Thiruporur")] * 4 + [("603103", "Chengalpattu")] * 2) == {}


def test_the_listing_resolver_places_through_the_sro_but_never_the_portal_city(monkeypatch):
    import scripts.resolve_places as rp
    gaz = Gazetteer(districts=["Chengalpattu"],
                    taluks=[("Chengalpattu", "Chengalpattu"), ("Tambaram", "Chengalpattu")],
                    villages=[("Kattankulathur", "Chengalpattu", "Chengalpattu"),
                              ("Nallur", "Chengalpattu", "Chengalpattu"),
                              ("Nallur", "Tambaram", "Chengalpattu")])
    base = {"taluk": None, "district": "Chengalpattu", "file_path": None,
            "registration_district": None}
    props = [
        {**base, "auction_id": "a1", "village": "Kattankalathur", "city": None,
         "registration_sub_district": "Chengalpet Joint-II SRO"},
        # the notice's own text gives the PIN
        {**base, "auction_id": "a3", "village": "Kattankalathur", "city": None,
         "registration_sub_district": None,
         "description": "Plot 4, Kattankalathur, Chengalpattu District - 603203"},
        # the portal city names a taluk, but it is only a witness
        {**base, "auction_id": "a2", "village": "Nallur", "city": "Tambaram",
         "registration_sub_district": None},
    ]
    monkeypatch.setattr(rp, "load_gazetteer", lambda: gaz)
    monkeypatch.setattr(rp, "load_properties", lambda: props)
    monkeypatch.setattr(rp, "notice_fallback", lambda: {})
    monkeypatch.setattr(rp, "load_decisions", lambda: [])
    monkeypatch.setattr(rp, "load_osm_aliases", lambda: {})
    monkeypatch.setattr(rp, "load_sro_taluks",
                        lambda: {sro_key("Chengalpet"): "Chengalpattu"})
    monkeypatch.setattr(rp, "load_pin_taluks", lambda: {"603203": "Chengalpattu"})
    written = []
    monkeypatch.setattr(rp, "write_back", lambda rows: written.extend(rows))
    monkeypatch.setattr(rp, "write_state", lambda *a: None)
    rp.run()
    by_id = {r["auction_id"]: r for r in written}
    assert (by_id["a1"]["village"], by_id["a1"]["taluk"], by_id["a1"]["village_source"]) \
        == ("Kattankulathur", "Chengalpattu", "sro-taluk")
    assert by_id["a2"]["village"] is None
    assert by_id["a2"]["village_status"] == "no-parent-taluk"
    assert (by_id["a3"]["village"], by_id["a3"]["village_source"]) == \
        ("Kattankulathur", "pin-taluk")
