"""Two files, one notice: page groups come from the listing's own file order.

The shapes are the corpus's own (AXIS-1…/AXIS-2… on three listings,
liq-1…/liq-2… on ten). DB-free: every function under test is pure.
"""
from __future__ import annotations

from pipeline.notice_pages import SEPARATOR, page_groups, stitch_pages


def _row(listing, filename, key, position):
    return {"listing": listing, "filename": filename,
            "content_key": key, "position": position}


def _pair(listings, p1="AXIS-1.jpg", p2="AXIS-2.jpg"):
    rows = []
    for a in listings:
        rows.append(_row(a, p1, "sha-1", 0))
        rows.append(_row(a, p2, "sha-2", 1))
    return rows


# ── grouping ────────────────────────────────────────────────────────────────

def test_two_pages_on_the_same_listings_form_one_group_in_portal_order():
    groups, ambiguous = page_groups(_pair(["824034", "824035", "824039"]))
    assert ambiguous == []
    assert groups == [{"pages": ["AXIS-1.jpg", "AXIS-2.jpg"], "twins": []}]


def test_order_follows_position_not_filename():
    rows = [_row("L1", "zzz.jpg", "sha-1", 0), _row("L1", "aaa.jpg", "sha-2", 1)]
    groups, _ = page_groups(rows)
    assert groups[0]["pages"] == ["zzz.jpg", "aaa.jpg"]


def test_a_listing_with_one_document_is_not_a_group():
    groups, ambiguous = page_groups([_row("L1", "only.jpg", "sha-1", 0)])
    assert groups == [] and ambiguous == []


def test_byte_twins_are_not_pages_of_each_other():
    rows = [_row("L1", "a.jpg", "sha-1", 0), _row("L1", "a-copy.jpg", "sha-1", 1)]
    groups, ambiguous = page_groups(rows)
    assert groups == [] and ambiguous == []


def test_a_byte_twin_of_a_page_rides_along_as_a_twin():
    rows = [_row("L1", "p1.jpg", "sha-1", 0), _row("L1", "p1-copy.jpg", "sha-1", 1),
            _row("L1", "p2.jpg", "sha-2", 2)]
    groups, _ = page_groups(rows)
    assert groups == [{"pages": ["p1.jpg", "p2.jpg"], "twins": ["p1-copy.jpg"]}]


def test_pages_must_share_exactly_the_same_listings():
    rows = _pair(["L1", "L2"]) + [_row("L3", "AXIS-1.jpg", "sha-1", 0)]
    groups, ambiguous = page_groups(rows)
    assert groups == []
    assert ambiguous == [{"filenames": ["AXIS-1.jpg", "AXIS-2.jpg"],
                          "reason": "listing sets differ: AXIS-1.jpg is on 3, AXIS-2.jpg is on 2"}]


def test_listings_must_agree_on_the_order():
    rows = [_row("L1", "a.jpg", "sha-1", 0), _row("L1", "b.jpg", "sha-2", 1),
            _row("L2", "a.jpg", "sha-1", 1), _row("L2", "b.jpg", "sha-2", 0)]
    groups, ambiguous = page_groups(rows)
    assert groups == []
    assert ambiguous[0]["reason"] == "order differs across listings"


def test_a_file_missing_from_downloads_list_is_ambiguous():
    rows = [_row("L1", "a.jpg", "sha-1", 0), _row("L1", "b.jpg", "sha-2", None)]
    groups, ambiguous = page_groups(rows)
    assert groups == []
    assert ambiguous == [{"filenames": ["a.jpg", "b.jpg"],
                          "reason": "b.jpg is not in the listing's downloads_list"}]


def test_groups_are_reported_once_however_many_listings_share_them():
    groups, _ = page_groups(_pair([str(n) for n in range(10)], "liq-1.jpg", "liq-2.jpg"))
    assert len(groups) == 1


# ── joining ─────────────────────────────────────────────────────────────────

def test_stitch_joins_in_order_with_the_separator_and_records_offsets():
    out = stitch_pages([{"filename": "p1", "markdown": "AB", "expected_lot_count": 14},
                        {"filename": "p2", "markdown": "CDE", "expected_lot_count": 10}])
    assert out["markdown"] == "AB" + SEPARATOR + "CDE"
    assert out["offsets"] == [0, 2 + len(SEPARATOR)]
    assert out["expected_lot_count"] == 24


def test_stitch_does_not_touch_page_text():
    out = stitch_pages([{"filename": "p1", "markdown": " A \n", "expected_lot_count": 1},
                        {"filename": "p2", "markdown": "\nB", "expected_lot_count": 1}])
    assert out["markdown"] == " A \n" + SEPARATOR + "\nB"


def test_lot_count_is_null_unless_every_page_has_one():
    out = stitch_pages([{"filename": "p1", "markdown": "A", "expected_lot_count": 3},
                        {"filename": "p2", "markdown": "B", "expected_lot_count": None}])
    assert out["expected_lot_count"] is None
