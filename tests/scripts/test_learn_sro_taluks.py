"""scripts/learn_sro_taluks: which taluk each sub-registrar office and PIN
points to, which taluks trade villages, and the hints reaching the listing
resolver."""
from __future__ import annotations

from pipeline.place_resolution import Gazetteer, sro_key
from scripts.learn_sro_taluks import (
    LEARN_FROM, MIN_LOTS, learn, learn_neighbours, learn_pins,
)


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
    assert not LEARN_FROM & {"sro-taluk", "city-taluk", "pin-taluk", "district-sound",
                             "neighbour-taluk"}


def test_a_pin_names_the_taluk_nearly_all_its_lots_sit_in():
    pairs = [("600017", "Guindy")] * 11 + [("600017", "Mambalam")]
    assert learn_pins(pairs) == {"600017": {"taluk": "Guindy", "lots": 12, "share": 0.917}}
    # a PIN that spans taluks names none
    assert learn_pins([("603103", "Thiruporur")] * 4 + [("603103", "Chengalpattu")] * 2) == {}


def test_two_taluks_trade_villages_when_enough_lots_name_one_and_sit_in_the_other():
    pairs = ([("Sriperumbudur", "Kundrathur")] * 80 + [("Kundrathur", "Sriperumbudur")] * 3
             + [("Chengalpattu", "Chengalpattu")] * 50
             + [("Hosur", "Krishnagiri")] * (MIN_LOTS - 1))
    assert learn_neighbours(pairs) == {"Kundrathur": {"Sriperumbudur": 83},
                                       "Sriperumbudur": {"Kundrathur": 83}}
    assert learn_neighbours([]) == {}


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
    monkeypatch.setattr(rp, "load_taluk_neighbours", lambda: {})
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


def test_the_listing_resolver_reads_a_field_s_pieces_and_the_neighbouring_taluks(monkeypatch):
    import scripts.resolve_places as rp
    gaz = Gazetteer(districts=["Kancheepuram"],
                    taluks=[("Sriperumbudur", "Kancheepuram"), ("Kundrathur", "Kancheepuram")],
                    villages=[("Irungattukottai", "Sriperumbudur", "Kancheepuram"),
                              ("Varatharajapuram", "Kundrathur", "Kancheepuram")])
    base = {"district": "Kancheepuram", "taluk": "Sriperumbudur", "file_path": None,
            "registration_district": None, "registration_sub_district": None,
            "city": None}
    props = [{**base, "auction_id": "a1", "village": "Irungattukottai Village Natham"},
             {**base, "auction_id": "a2", "village": "Varadarajapuram"}]
    monkeypatch.setattr(rp, "load_gazetteer", lambda: gaz)
    monkeypatch.setattr(rp, "load_properties", lambda: props)
    monkeypatch.setattr(rp, "notice_fallback", lambda: {})
    monkeypatch.setattr(rp, "load_decisions", lambda: [])
    monkeypatch.setattr(rp, "load_osm_aliases", lambda: {})
    monkeypatch.setattr(rp, "load_sro_taluks", lambda: {})
    monkeypatch.setattr(rp, "load_pin_taluks", lambda: {})
    monkeypatch.setattr(rp, "load_taluk_neighbours",
                        lambda: {"Sriperumbudur": ("Kundrathur",),
                                 "Kundrathur": ("Sriperumbudur",)})
    written = []
    monkeypatch.setattr(rp, "write_back", lambda rows: written.extend(rows))
    monkeypatch.setattr(rp, "write_state", lambda *a: None)
    rp.run()
    by_id = {r["auction_id"]: r for r in written}
    assert (by_id["a1"]["village"], by_id["a1"]["village_source"]) == \
        ("Irungattukottai", "village-pieces")
    assert (by_id["a2"]["village"], by_id["a2"]["taluk"], by_id["a2"]["village_source"]) == \
        ("Varatharajapuram", "Kundrathur", "neighbour-taluk")
