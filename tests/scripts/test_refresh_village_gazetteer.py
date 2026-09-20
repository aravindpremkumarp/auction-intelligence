"""The gazetteer refresh: what it counts as missing, and what it refuses to touch.

Pure tests — the diff is a function of two dicts, so no DB is needed.
"""
from __future__ import annotations

import pytest

from pipeline.place_resolution import normalize_place
from scripts.refresh_village_gazetteer import _header_key, diff, read_source_csv

TALUKS = {normalize_place(t): (d, t) for d, t in [
    ("Tiruvallur", "Avadi"),
    ("Tiruvallur", "Poonamallee"),
    ("Chengalpattu", "Tambaram"),
]}

GRAPH = {
    ("Tiruvallur", "Avadi"): {normalize_place("Morai"): "Morai"},
    ("Chengalpattu", "Tambaram"): {normalize_place("Sembakkam"): "Sembakkam"},
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


def test_a_village_only_the_graph_has_is_counted_never_deleted():
    """A district-scoped export is shorter than the graph by design. Reporting
    the difference is useful; acting on it would empty the gazetteer from a
    truncated download."""
    r = diff([_row("Tiruvallur", "Avadi", "Morai")], GRAPH, TALUKS)
    assert r["only_in_graph"] == 1          # Tambaram's Sembakkam
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
