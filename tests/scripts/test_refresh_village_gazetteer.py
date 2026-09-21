"""The gazetteer refresh: what it counts as missing, and what it refuses to touch.

Pure tests — the diff is a function of two dicts, so no DB is needed.
"""
from __future__ import annotations

import pytest

from pipeline.place_resolution import Gazetteer, normalize_place
from scripts.refresh_village_gazetteer import _header_key, diff, read_source_csv

#: The graph side of the taluk lookup, as the resolver's own index. The diff
#: resolves the input's taluk through ``Gazetteer.taluk`` — aliases, the fold,
#: then similarity — because a folded-exact lookup rejects 11% of LGD's own
#: export for spelling a taluk the graph holds under another name.
TALUKS = Gazetteer(
    districts=["Tiruvallur", "Chengalpattu", "Cuddalore", "Nilgiris", "Ariyalur"],
    taluks=[
        ("Avadi", "Tiruvallur"),
        ("Poonamallee", "Tiruvallur"),
        ("Tambaram", "Chengalpattu"),
        ("Vridhachalam", "Cuddalore"),
        ("Pandalur", "Nilgiris"),
        ("Andimadam", "Ariyalur"),
    ])

GRAPH = {
    ("Tiruvallur", "Avadi"): {normalize_place("Morai"): "Morai"},
    ("Chengalpattu", "Tambaram"): {normalize_place("Sembakkam"): "Sembakkam"},
    ("Ariyalur", "Andimadam"): {normalize_place("Athukurichi"): "Athukurichi"},
}


def _row(district, taluk, village, **extra):
    return {"district": district, "taluk": taluk, "village": village, **extra}


def test_a_village_the_graph_lacks_is_the_thing_this_finds():
    """Thirumullaivoyal is a real village of Avadi taluk and is absent from the
    live gazetteer — the failure that made this script necessary."""
    r = diff([_row("Tiruvallur", "Avadi", "Thirumullaivoyal")], GRAPH, TALUKS)
    assert [m["village"] for m in r["missing"]] == ["Thirumullaivoyal"]
    assert r["present"] == 0


def test_a_spelling_variant_of_a_village_already_held_is_not_added_again():
    """The diff folds names the way the resolver does. Without that, "Morrai"
    reads as missing and lands beside "Morai" as a duplicate the resolver would
    then have to disambiguate."""
    r = diff([_row("Tiruvallur", "Avadi", "Morrai")], GRAPH, TALUKS)
    assert r["missing"] == []
    assert r["present"] == 1


def test_the_taluk_is_matched_on_the_same_fold_as_everything_else():
    """An export spelling Tambaram's district "Chengalpet" still resolves: the
    taluk carries the official district, so the input's spelling never decides
    where a village lands."""
    r = diff([_row("Chengalpet", "Thambaram", "Chitlapakkam")], GRAPH, TALUKS)
    assert r["missing"][0]["district"] == "Chengalpattu"
    assert r["missing"][0]["taluk"] == "Tambaram"


def test_a_taluk_the_graph_does_not_hold_is_reported_not_invented():
    r = diff([_row("Kerala", "Ottappalam", "Chalavara")], GRAPH, TALUKS)
    assert r["missing"] == []
    assert r["unknown_taluk"] == {"Ottappalam [Kerala]": 1}


def test_an_official_exports_taluk_spelling_still_lands_on_the_graphs():
    """LGD writes "Virudhachalam" where the graph holds "Vridhachalam", and
    "Panthalur" where it holds "Pandalur". Both are the same taluk. On a strict
    fold these read as taluks the graph has no hierarchy for, and 2,277 rows of
    LGD's Tamil Nadu export — 11% of it, across 38 taluks — were dropped that
    way. Resolving through the pipeline's own matcher is what recovers them."""
    rows = [_row("Cuddalore", "Virudhachalam", "Pennadam"),
            _row("The Nilgiris", "Panthalur", "Cherambadi")]
    r = diff(rows, GRAPH, TALUKS)
    assert r["unknown_taluk"] == {}
    assert [(m["district"], m["taluk"]) for m in r["missing"]] == [
        ("Cuddalore", "Vridhachalam"), ("Nilgiris", "Pandalur")]


def test_a_similar_taluk_in_another_district_than_the_row_states_is_refused():
    """The looser match is only safe because the export names its district too.
    A hit landing somewhere the row does not claim is the failure the taluk rule
    exists to prevent — a property filed into the wrong district — so it is
    reported like any other unplaceable row rather than written."""
    r = diff([_row("Chengalpattu", "Avadi", "Thirumullaivoyal")], GRAPH, TALUKS)
    assert r["missing"] == []
    assert r["unknown_taluk"] == {"Avadi [Chengalpattu]": 1}


def test_a_village_held_under_another_spelling_is_reported_not_added():
    """The fold catches "Morrai" against "Morai" — doubled consonants collapse.
    It does not catch "Authukurichi" against the held "Athukurichi", which is
    the same village of Andimadam and one of 3,081 such pairs in LGD's Tamil
    Nadu export. Writing it would put two names scoring 95 against each other
    inside one taluk, at which point FUZZY_MARGIN leaves the resolver unable to
    choose and it refuses a village it places correctly today. So it is
    reported, with the name it matched."""
    r = diff([_row("Ariyalur", "Andimadam", "Authukurichi")], GRAPH, TALUKS)
    assert r["missing"] == []
    assert [(v["village"], v["held"]) for v in r["variants"]] == [
        ("Authukurichi", "Athukurichi")]


def test_a_village_the_taluk_really_lacks_is_still_an_addition():
    """The variant check must not swallow the thing this script exists to find:
    Selaiyur resembles nothing in Tambaram, so it is added."""
    r = diff([_row("Chengalpattu", "Tambaram", "Selaiyur")], GRAPH, TALUKS)
    assert [m["village"] for m in r["missing"]] == ["Selaiyur"]
    assert r["variants"] == []


def test_the_village_a_variant_matched_is_not_also_reported_as_graph_only():
    """The source does list Sembakkam — under another spelling. Counting it
    graph-only would report the export as short of the graph by villages it
    names."""
    r = diff([_row("Ariyalur", "Andimadam", "Authukurichi")], GRAPH, TALUKS)
    # Morai and Sembakkam, which this one-row input genuinely does not name.
    # Athukurichi is not among them: the variant accounts for it.
    assert r["only_in_graph"] == 2


def test_a_village_only_the_graph_has_is_counted_never_deleted():
    """A district-scoped export is shorter than the graph by design. Reporting
    the difference is useful; acting on it would empty the gazetteer from a
    truncated download."""
    r = diff([_row("Tiruvallur", "Avadi", "Morai")], GRAPH, TALUKS)
    assert r["only_in_graph"] == 2         # Tambaram's Sembakkam, Andimadam's Athukurichi
    assert r["missing"] == []


def test_one_name_listed_twice_is_added_once():
    rows = [_row("Tiruvallur", "Avadi", "Paruthipattu"),
            _row("Tiruvallur", "Avadi", "Paruthipattu")]
    assert len(diff(rows, GRAPH, TALUKS)["missing"]) == 1


def test_a_district_filter_narrows_both_sides_of_the_diff():
    """Pointing a run at the thin taluks must not report every other district's
    villages as graph-only — the filter applies to the graph side too."""
    rows = [_row("Tiruvallur", "Avadi", "Thirumullaivoyal"),
            _row("Tiruvallur", "Avadi", "Morai"),
            _row("Chengalpattu", "Tambaram", "Selaiyur")]
    r = diff(rows, GRAPH, TALUKS, districts={"Tiruvallur"})
    assert [m["village"] for m in r["missing"]] == ["Thirumullaivoyal"]
    # Avadi is fully accounted for, and Tambaram's Sembakkam — which the input
    # does not list — is out of scope rather than reported as a graph-only row.
    assert r["only_in_graph"] == 0


@pytest.mark.parametrize("header,expected", [
    ("District Name", "district"),
    ("Sub-District Name", "taluk"),      # LGD's spelling
    ("subdistrict_name", "taluk"),
    ("Village Name", "village"),
    ("Taluk", "taluk"),
    ("State Name", None),
    # Two different numbers, two different columns. `village_code` is the
    # graph's within-taluk revenue serial ("008"); LGD's is a six-digit national
    # identifier on an unrelated scheme. A source code landing in the wrong one
    # leaves a property that means two things depending on the row.
    ("Village Code", "village_code"),
    ("LGD Code", "lgd_village_code"),
    ("Village LGD Code", "lgd_village_code"),
])
def test_export_headers_are_recognised_without_configuration(header, expected):
    assert _header_key(header) == expected


def test_a_row_missing_half_its_hierarchy_is_skipped(tmp_path):
    p = tmp_path / "v.csv"
    p.write_text("District Name,Sub-District Name,Village Name\n"
                 "Tiruvallur,Avadi,Thirumullaivoyal\n"
                 "Tiruvallur,,Paruthipattu\n", encoding="utf-8")
    assert [r["village"] for r in read_source_csv(str(p))] == ["Thirumullaivoyal"]


def test_a_csv_without_the_three_names_is_refused(tmp_path):
    p = tmp_path / "v.csv"
    p.write_text("State Name,Population\nTamil Nadu,72147030\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        read_source_csv(str(p))
