"""The loader must not undo a human's price correction.

`BATCH_QUERY` re-SETs every portal field on each load. Money is the one field
corrected against the sale notice — 766811 lost a leading digit from both price
and EMD, 795611 was out by a factor of ten — so an unconditional SET quietly
reverts that work, and nothing in the loader knows it happened.

These assert the guard's shape rather than run Cypher: the query is a module
constant, and a regression here is someone deleting a CASE, which reads as
harmless in a diff.
"""
from __future__ import annotations

import re

from scripts.load_tn_to_neo4j import BATCH_QUERY

#: The four fields a correction owns once `price_corrected_at` is stamped.
GUARDED = ("reserve_price_raw", "reserve_price_num", "emd_raw", "emd_num")


def _assignment(field: str) -> str:
    """The SET clause for one AuctionProperty field, up to the next one."""
    m = re.search(rf"a\.{field}\s*=\s*(.*?)(?=,\n\s+(?://.*\n\s+)*a\.|\n\n)",
                  BATCH_QUERY, re.S)
    assert m, f"no assignment found for {field}"
    return m.group(1)


def test_every_money_field_is_guarded():
    for f in GUARDED:
        assert "price_corrected_at IS NULL" in _assignment(f), \
            f"{f} would overwrite a correction"


def test_a_guarded_field_keeps_its_own_value_when_corrected():
    for f in GUARDED:
        assert f"a.{f} END" in _assignment(f).replace("\n", " ").replace(
            "  ", " ") or f"ELSE a.{f}" in _assignment(f), \
            f"{f} does not fall back to the stored correction"


def test_an_uncorrected_listing_still_takes_the_portal_value():
    for f in GUARDED:
        assert f"r.{f}" in _assignment(f), \
            f"{f} no longer reads the portal value at all"


def test_the_portal_figure_is_still_recorded_on_a_corrected_listing():
    """A scrape that fixes its own error must be visible, not swallowed."""
    assert "a.portal_reserve_price_num" in BATCH_QUERY
    assert "a.portal_emd_num" in BATCH_QUERY


def test_unrelated_portal_fields_are_not_guarded():
    """The guard is deliberately narrow — title and description have no
    correction workflow, and freezing them would strand real portal updates."""
    for f in ("title", "url", "service_provider"):
        assert "price_corrected_at" not in _assignment(f)
