"""Guard: a flat's UDS parent-plot extent must not be recorded as its own area.

A flat sits on a larger plot and owns only an undivided share of it. The
parent-plot extent (Schedule A, e.g. "Plot No.3 measuring 2257 sq.ft") belongs
ONLY in uds_parent_extent — never in total_area/extent_sqft, which describe the
property's OWN land. Echoing it there makes a 760 sq.ft flat look like 2257 sq.ft.
Pure test (no langextract / API key): drives validate() directly.
"""
from __future__ import annotations

from types import SimpleNamespace

from pipeline.validators import validate


def E(cls, lot="1", **attrs):
    a = dict(attrs)
    a["lot_index"] = lot
    return SimpleNamespace(extraction_class=cls, extraction_text="",
                           attributes=a,
                           char_interval=SimpleNamespace(start_pos=0, end_pos=1))


def _codes(ex):
    return {i["code"] for i in validate(ex)["issues"]}


def test_parent_extent_echoed_as_own_area_is_flagged():
    # the real 0195f7d6 shape: 2257 appears both as the property's total_area and
    # as the UDS parent extent.
    ex = [E("extent", total_area="2257 sq.ft", extent_sqft=2257),
          E("extent", undivided_share="365 sq.ft", uds_parent_extent="2257 sq.ft"),
          E("extent", built_up_area="760 sq.ft", extent_sqft=760)]
    assert "uds_parent_as_own_area" in _codes(ex)


def test_clean_flat_extents_not_flagged():
    # UDS + built-up only, parent extent stays in uds_parent_extent -> no flag.
    ex = [E("extent", undivided_share="365 sq.ft", uds_parent_extent="2257 sq.ft"),
          E("extent", built_up_area="760 sq.ft", extent_sqft=760)]
    assert "uds_parent_as_own_area" not in _codes(ex)


def test_plain_land_total_area_not_flagged():
    # pure land parcel: total_area with no UDS parent is correct.
    ex = [E("extent", total_area="2257 sq.ft", extent_sqft=2257)]
    assert "uds_parent_as_own_area" not in _codes(ex)


def test_same_number_across_different_lots_does_not_cross_trigger():
    # lot 1 is a flat (parent 2257); lot 2 is land that happens to be 2257 sq.ft.
    # The check is per-lot, so no false positive.
    ex = [E("extent", lot="1", undivided_share="365 sq.ft",
            uds_parent_extent="2257 sq.ft"),
          E("extent", lot="2", total_area="2257 sq.ft", extent_sqft=2257)]
    assert "uds_parent_as_own_area" not in _codes(ex)
