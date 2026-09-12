"""api.review.queries._place_panels: the portal disagreement, broken down.

`place_portal_conflict` fires on 527 of 2,964 listings and 459 of those are the
2019 district splits or the Chennai metro. The panel used to show the one
number, so there was no way to see that the actionable part is ~69. These tests
pin the breakdown, and the fallback for a graph where the kind has never been
written.
"""
from __future__ import annotations

import pytest

from api.review import queries as Q


@pytest.fixture
def counts() -> dict:
    """The live shape at the time this was written."""
    return {"total": 2964, "d": 2700, "t": 2049, "v": 1129,
            "portal": 527, "notice": 304}


def _install(monkeypatch, counts: dict, kind_rows: list[dict],
             stop_rows: list[dict] | None = None) -> None:
    """Answer the panel's two reads by which one it is.

    `_place_panels` runs one `_count_query` and two `run_read_query` calls, and
    they are told apart by what the Cypher selects — matching on the returned
    alias rather than on call order, so reordering the function does not
    silently feed the village query the conflict rows.
    """
    monkeypatch.setattr(Q, "_count_query", lambda *a, **k: counts)

    def fake_read(cypher, *args, **kwargs):
        if "place_portal_conflict_kind" in cypher:
            return kind_rows
        if "place_village_status" in cypher:
            return stop_rows or []
        raise AssertionError(f"unexpected query: {cypher[:60]}")

    monkeypatch.setattr(Q, "run_read_query", fake_read)


def _disagreements(panels: list[dict]) -> dict:
    return next(p for p in panels if p["title"] == "Disagreements")


def test_the_kinds_are_counted_apart(monkeypatch, counts):
    _install(monkeypatch, counts, [
        {"t": "portal-names-parent", "n": 280},
        {"t": "no-notice-district", "n": 264},
        {"t": "metro-ring", "n": 181},
        {"t": "unexplained", "n": 40},
        {"t": "notice-names-parent", "n": 29},
    ])
    rows = _disagreements(Q._place_panels())["rows"]
    labels = [r["label"] for r in rows]

    # The notice-vs-taluk row keeps its place at the top.
    assert rows[0]["count"] == 304
    # Every kind reads as English, not as a status code.
    assert not any(lbl.startswith("portal-") or lbl.startswith("metro-")
                   for lbl in labels)
    assert any("split from" in lbl for lbl in labels)
    assert any("Chennai metro" in lbl for lbl in labels)
    assert any("read the notice" in lbl for lbl in labels)


def test_a_missing_side_is_not_shown_as_a_disagreement(monkeypatch, counts):
    """264 listings resolved no district. That is a coverage number the first
    panel already reports; repeating it here would double-count one gap."""
    _install(monkeypatch, counts, [
        {"t": "no-notice-district", "n": 264},
        {"t": "no-portal-district", "n": 43},
        {"t": "unexplained", "n": 40},
    ])
    rows = _disagreements(Q._place_panels())["rows"]
    assert [r["count"] for r in rows] == [304, 40]


def test_agree_never_reaches_the_panel(monkeypatch, counts):
    """The query excludes it, but the panel must not depend on that — an
    'agree' row in a Disagreements panel is a contradiction, not a big number.
    """
    _install(monkeypatch, counts, [
        {"t": "unexplained", "n": 40},
    ])
    rows = _disagreements(Q._place_panels())["rows"]
    assert all("agree" not in r["label"] for r in rows)


def test_before_the_kind_is_written_the_old_number_still_shows(monkeypatch,
                                                              counts):
    """`place_portal_conflict_kind` is set by the next `resolve_places` run. On
    a graph that has not had one, an empty breakdown must fall back to the
    boolean rather than render a panel reading 'no conflicts'."""
    _install(monkeypatch, counts, [])
    rows = _disagreements(Q._place_panels())["rows"]
    assert [r["count"] for r in rows] == [304, 527]


def test_an_unknown_kind_is_shown_rather_than_dropped(monkeypatch, counts):
    """A code added to the resolver but not to SAID must still be counted —
    silently losing rows is how a backlog shrinks for the wrong reason."""
    _install(monkeypatch, counts, [{"t": "some-new-kind", "n": 7}])
    rows = _disagreements(Q._place_panels())["rows"]
    assert rows[1] == {"label": "some-new-kind", "count": 7,
                       "pct": round(7 / 2964 * 100, 1), "href": None}
