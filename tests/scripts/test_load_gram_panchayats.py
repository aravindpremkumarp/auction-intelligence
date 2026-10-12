"""scripts/load_gram_panchayats: matching LGD rows to held villages."""
from pipeline.place_resolution import Gazetteer, normalize_place
from scripts.load_gram_panchayats import _WRITE, plan

TALUKS = Gazetteer(districts=["Ariyalur"], taluks=[("Andimadam", "Ariyalur")])
GRAPH = {("Ariyalur", "Andimadam"): {normalize_place("Kuvagam"): "Kuvagam"}}


def _row(village, gp, code, gp_code):
    return {"district": "Ariyalur", "taluk": "Andimadam", "village": village,
            "lgd_village_code": code, "gram_panchayat": gp, "gram_panchayat_code": gp_code}


def test_a_village_under_two_panchayats_gets_both():
    todo, _ = plan([_row("Kuvagam", "Kuvagam", "636359", "226346"),
                    _row("Kuvagam", "Vallam", "636359", "226358")], GRAPH, TALUKS)
    assert sorted(r["gp"] for r in todo) == ["Kuvagam", "Vallam"]


def test_unknown_villages_are_counted_not_created():
    todo, stats = plan([_row("Nowhere", "X", "1", "2")], GRAPH, TALUKS)
    assert todo == [] and stats["village not in graph"] == 1
    assert "MERGE (v:RevenueVillage" not in _WRITE


def test_an_urban_row_lends_only_its_code():
    todo, _ = plan([_row("Kuvagam", "", "636359", "")], GRAPH, TALUKS)
    assert todo[0]["gp_code"] is None and todo[0]["lgd_village_code"] == "636359"


def test_lgd_code_is_never_overwritten():
    assert "coalesce(v.lgd_village_code, row.lgd_village_code)" in _WRITE
