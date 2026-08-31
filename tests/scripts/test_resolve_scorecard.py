"""The standing scorecard: shape, comparability, and the not-measured guard.

Pure logic only — `collect()` is the one function that touches Neo4j and is
exercised against a stubbed reader.
"""
from __future__ import annotations

import json

import scripts.resolve_scorecard as SC


# ── metric ───────────────────────────────────────────────────────────────────

def test_metric_carries_the_share_not_just_the_count():
    """A snapshot read months later must mean the same thing, so the
    percentage is stored rather than recomputed against a denominator whose
    definition may have moved."""
    assert SC.metric(2804, 2964) == {"value": 2804, "total": 2964, "pct": 94.6}


def test_metric_without_a_total_is_a_bare_count():
    assert SC.metric(177) == {"value": 177}


def test_a_zero_denominator_does_not_divide_by_zero():
    assert SC.metric(0, 0)["pct"] == 0.0


# ── the not-measured guard ───────────────────────────────────────────────────

def _card(monkeypatch, *, area_key_exists):
    """collect() against a stubbed graph. Only the fields each query reads
    are returned; every metric falls back to 0 for anything absent."""
    # Dispatch on each query's own output names, not on its opening MATCH:
    # the linkage and price queries share `MATCH (a:AuctionProperty) RETURN
    # count(a) AS total`, and keying on that fed the price query the linkage
    # stub (flagged came back 0).
    def fake_one(cypher, params=None):
        if "db.propertyKeys" in cypher:
            return {"price": True, "area": area_key_exists}
        if "AS linked" in cypher:
            return {"total": 2964, "linked": 2804}
        if "AS area_pairs" in cypher:
            return {"area_pairs": 2176, "price_pairs": 2761}
        if "a.area_agreement IS NOT NULL" in cypher:
            return {"flagged": 0, "critical": 0}
        if "a.price_agreement IS NOT NULL" in cypher:
            return {"total": 2964, "flagged": 131, "critical": 35}
        return {}

    monkeypatch.setattr(SC, "one", fake_one)
    return SC.collect()


def test_a_check_that_never_ran_is_not_reported_as_zero(monkeypatch):
    """Both writers clear every flag and rewrite, so "the corpus agrees" and
    "the pipeline has not run since this check shipped" both look like 0
    findings. Reading `clean` off a number nobody computed is the failure
    this guards."""
    card = _card(monkeypatch, area_key_exists=False)
    area = card["sections"]["agreement"]["area_disagreements"]
    assert area["not_yet_run"] is True
    assert "NOT MEASURED" in area["note"]


def test_a_check_that_has_run_reports_its_zero_honestly(monkeypatch):
    """db.propertyKeys is append-only, so the key surviving a pass that found
    nothing is what separates this case from the one above."""
    card = _card(monkeypatch, area_key_exists=True)
    area = card["sections"]["agreement"]["area_disagreements"]
    assert "not_yet_run" not in area
    assert area == {"value": 0, "total": 2176, "pct": 0.0,
                    "note": "of the confirmed pairs where both sides carry a size"}


def test_a_check_that_has_run_keeps_its_denominator(monkeypatch):
    card = _card(monkeypatch, area_key_exists=True)
    price = card["sections"]["agreement"]["price_disagreements"]
    assert (price["value"], price["total"]) == (131, 2761)


def test_linkage_counts_the_whole_corpus(monkeypatch):
    """A stray `count { ... }` beside the aggregations became a GROUPING KEY
    and made this read 2,804 of 2,804 (100%) — the denominator silently
    shrank to one group."""
    card = _card(monkeypatch, area_key_exists=True)
    linked = card["sections"]["linkage"]["listings_linked_to_a_lot"]
    assert (linked["value"], linked["total"], linked["pct"]) == (2804, 2964, 94.6)


# ── comparison between snapshots ─────────────────────────────────────────────

def test_flatten_keys_are_stable_across_sections():
    card = {"sections": {"linkage": {"a": {"value": 1}},
                         "places": {"b": {"value": 2}}}}
    assert SC._flat(card) == {"linkage.a": {"value": 1}, "places.b": {"value": 2}}


def test_render_survives_a_previous_snapshot_missing_a_metric(capsys):
    """A metric added after the baseline was taken must print, not crash."""
    card = {"generated_at": "2026-09-01T00:00:00Z",
            "sections": {"linkage": {"new_metric": {"value": 5}}}}
    SC.render(card, {"generated_at": "2026-08-31T00:00:00Z", "sections": {}})
    assert "new_metric" in capsys.readouterr().out


def test_render_shows_movement_against_the_baseline(capsys):
    card = {"generated_at": "2026-09-01T00:00:00Z",
            "sections": {"linkage": {"m": {"value": 12}}}}
    prev = {"generated_at": "2026-08-31T00:00:00Z",
            "sections": {"linkage": {"m": {"value": 20}}}}
    SC.render(card, prev)
    assert "-8 since 2026-08-31" in capsys.readouterr().out


def test_a_damaged_baseline_does_not_lose_this_run(monkeypatch, tmp_path, capsys):
    """The point of the run is today's numbers; a broken --compare file must
    cost the delta, not the snapshot."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    out = tmp_path / "card.json"
    monkeypatch.setattr(SC, "collect", lambda: {
        "generated_at": "2026-09-01T00:00:00Z",
        "sections": {"linkage": {"m": {"value": 1}}}})
    assert SC.main(["--json", str(out), "--compare", str(bad)]) == 0
    assert "ignoring --compare" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["sections"]["linkage"]["m"]["value"] == 1
