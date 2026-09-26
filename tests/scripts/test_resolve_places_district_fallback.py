"""scripts/resolve_places: the district-wide second chance for a listing whose
notice gave a village and a district but no usable taluk — the rule lot_place
already applies to the lot the listing points at."""
from __future__ import annotations

from pipeline.place_resolution import Gazetteer, resolve_place
from scripts.resolve_places import district_second_chance


def _gaz() -> Gazetteer:
    return Gazetteer(
        districts=["Tiruppur", "Madurai"],
        taluks=[("Tiruppur North", "Tiruppur"), ("Tiruppur South", "Tiruppur"),
                ("Usilampatti", "Madurai"), ("Peraiyur", "Madurai")],
        villages=[("Nallur", "Tiruppur South", "Tiruppur"),
                  ("Usilampatti", "Usilampatti", "Madurai"),
                  ("Usilampatti", "Peraiyur", "Madurai")],
    )


def test_a_village_unique_in_its_district_is_placed_with_its_own_taluk():
    gaz = _gaz()
    # "Tiruppur" is written in the taluk field: it names the district, and the
    # gazetteer has split the taluk, so resolve_place refuses to guess one.
    res = resolve_place(gaz, district="Tiruppur", taluk="Tiruppur", village="Nallur")
    assert res["village_status"] == "no-parent-taluk" and res["village"] is None
    out = district_second_chance(gaz, res, "Nallur")
    assert (out["village"], out["taluk"], out["district"]) == \
        ("Nallur", "Tiruppur South", "Tiruppur")
    assert out["village_status"] == "resolved"
    assert out["village_source"] == "district"      # provenance stays separable


def test_a_name_two_villages_share_is_refused():
    gaz = _gaz()
    res = resolve_place(gaz, district="Madurai", village="Usilampatti")
    assert res["village_status"] == "no-parent-taluk"
    out = district_second_chance(gaz, res, "Usilampatti")
    assert out["village"] is None and out["village_status"] == "no-parent-taluk"


def test_only_a_no_parent_taluk_result_is_touched():
    gaz = _gaz()
    # no district: nothing to search within
    res = resolve_place(gaz, village="Nallur")
    assert district_second_chance(gaz, res, "Nallur") == res
    # an already-resolved result is returned unchanged
    done = {"village": "Nallur", "taluk": "Tiruppur South", "district": "Tiruppur",
            "village_status": "resolved", "village_source": "taluk"}
    assert district_second_chance(gaz, done, "Nallur") is done
    # only the gazetteer's own folding (doubled letters, spacing) counts as the
    # same name; a different spelling is not matched loosely at district scope
    res2 = resolve_place(gaz, district="Tiruppur", taluk="Tiruppur", village="Nellur")
    assert district_second_chance(gaz, res2, "Nellur")["village"] is None
