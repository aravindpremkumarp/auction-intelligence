"""Guards for the full_description completeness invariant (pipeline/validators).

The design rule (source of truth): full_description is the verbatim union of a
lot's descriptive spans, so every property/location/extent/boundary/identifier/
schedule span must sit INSIDE that lot's full_description span. When one falls
outside, full_description was truncated before that detail. Pure test — builds
extraction objects directly, no langextract / API key.
"""
from __future__ import annotations

from types import SimpleNamespace

from pipeline.validators import full_description_coverage, validate


def E(cls, start, end, lot="1", text="", **attrs):
    """Minimal extraction stand-in with a char span (None start = ungrounded)
    and optional extraction_text (drives the derivability/text arm)."""
    ci = None if start is None else SimpleNamespace(start_pos=start, end_pos=end)
    a = dict(attrs)
    if lot is not None:
        a["lot_index"] = lot
    return SimpleNamespace(extraction_class=cls, extraction_text=text,
                           attributes=a, char_interval=ci)


def _codes(extractions):
    return {i["code"] for i in validate(extractions)["issues"]}


def test_complete_description_is_not_flagged():
    ex = [E("full_description", 100, 300), E("property", 100, 180),
          E("location", 200, 240), E("boundary", 250, 270),
          E("identifier", 120, 130)]
    cov = full_description_coverage(ex)
    assert cov["lots_incomplete"] == {}
    assert cov["lots_missing_full_description"] == []
    assert "full_description_incomplete" not in _codes(ex)


def test_span_outside_full_description_flags_incomplete():
    # boundary sits AFTER full_description ends -> fd truncated before boundaries.
    ex = [E("full_description", 100, 200), E("property", 100, 180),
          E("boundary", 250, 270)]
    cov = full_description_coverage(ex)
    assert cov["lots_incomplete"] == {"1": ["boundary"]}
    assert "full_description_incomplete" in _codes(ex)


def test_span_starting_before_full_description_flags():
    # an identifier that begins before fd starts, whose text is NOT in fd, is
    # outside.
    ex = [E("full_description", 100, 300, text="all that parcel of land"),
          E("identifier", 40, 60, text="Survey No.999")]
    assert full_description_coverage(ex)["lots_incomplete"] == {"1": ["identifier"]}


def test_duplicate_mention_outside_span_but_text_inside_is_covered():
    # the derivability arm: a value repeated at an earlier position (span outside)
    # but whose text also appears INSIDE full_description is still derivable.
    fd = "all that parcel bearing Flat No. G1, Block No.18 with boundaries"
    ex = [E("full_description", 100, 300, text=fd),
          E("identifier", 40, 51, text="Flat No. G1"),   # span before fd, text inside
          E("identifier", 52, 63, text="Block No.18")]
    assert full_description_coverage(ex)["lots_incomplete"] == {}


def test_missing_full_description_flagged():
    ex = [E("property", 100, 180), E("location", 200, 240)]
    cov = full_description_coverage(ex)
    assert cov["lots_missing_full_description"] == ["1"]
    assert "missing_full_description" in _codes(ex)


def test_multi_lot_coverage_is_per_lot():
    # lot 1 fully covered; lot 2's boundary falls outside lot 2's fd.
    ex = [E("full_description", 100, 300, lot="1"), E("boundary", 250, 270, lot="1"),
          E("full_description", 400, 600, lot="2"), E("boundary", 700, 720, lot="2")]
    cov = full_description_coverage(ex)
    assert cov["lots_incomplete"] == {"2": ["boundary"]}
    assert cov["lots_with_description"] == 2


def test_ungrounded_spans_are_skipped():
    # an ungrounded boundary (no char positions) can't be checked -> no false flag.
    ex = [E("full_description", 100, 300), E("boundary", None, None)]
    cov = full_description_coverage(ex)
    assert cov["lots_incomplete"] == {}
    assert cov["lots_missing_full_description"] == []


def test_notice_level_classes_dont_require_coverage():
    # secured_creditor / full_terms aren't property description -> nothing to cover.
    ex = [E("secured_creditor", 0, 20, lot=None), E("full_terms", 300, 900, lot=None)]
    cov = full_description_coverage(ex)
    assert cov["lots_with_description"] == 0
    assert cov["lots_missing_full_description"] == []


def test_stats_expose_coverage_counts():
    ex = [E("full_description", 100, 200), E("boundary", 250, 270)]
    stats = validate(ex)["stats"]
    assert stats["full_description_incomplete_lots"] == 1
    assert stats["lots_missing_full_description"] == 0


# ── lot_index arrives as a number as readily as a string ────────────────────
# The prompt asks for `lot_index=N`, and the model obliges with a JSON number
# about as often as a string. The "1" default here is a string, so a notice
# carrying both used to make `sorted(missing_fd)` raise
# `'<' not supported between instances of 'int' and 'str'` — which is not a
# flagged issue but an exception, so load_extractions counted the whole
# document as failed and wrote nothing. That is exactly how
# 47bde643-ce3d-45fe-8c0f-6030a96f566c17820352146257.jpg was lost from batch
# B30. pipeline.apply_extractions.group_lots has always normalised with str();
# these pin the validator to the same rule.

def test_numeric_and_string_lot_index_do_not_crash_the_validator():
    ex = [E("property", 100, 180, lot="1"), E("property", 400, 480, lot=2)]
    cov = full_description_coverage(ex)          # must not raise
    assert cov["lots_missing_full_description"] == ["1", "2"]


def test_numeric_lot_index_is_the_same_lot_as_its_string_form():
    """1 and "1" are one lot, not two. Before the fix the set held both and
    every per-lot count downstream was inflated."""
    ex = [E("full_description", 100, 300, lot=1), E("property", 100, 180, lot="1")]
    cov = full_description_coverage(ex)
    assert cov["lots_with_description"] == 1
    assert cov["lots_missing_full_description"] == []


def test_validate_counts_one_lot_for_mixed_index_types():
    ex = [E("property", 100, 180, lot=1), E("location", 200, 240, lot="1")]
    assert validate(ex)["stats"]["lots"] == 1


# ── what a detail outside the block does NOT prove (2026-10 sweep) ──────────
# 78% of the notices this check flagged were flagged only by details that are
# no sign of a cut-short block. Each test pins one group of the sweep — and the
# real truncation it must still catch. Spans come from a page, so each detail
# sits where it would in a notice.

def _at(page, cls, text, lot="1", **attrs):
    s = page.index(text)
    return E(cls, s, s + len(text), lot=lot, text=text, **attrs)


def _block(page, text, lot="1"):
    return _at(page, "full_description", text, lot=lot)


FD1 = ("All that piece and parcel of land bearing Door No.92/2, S.F.No.179/6, "
       "Ponmeni Village, Madurai South Taluk, measuring 1200 sq.ft.")


def test_boundaries_after_the_block_are_still_a_truncation():
    page = FD1 + " Bounded by: North by: Vacant Land, South by: 30 feet road."
    ex = [_block(page, FD1), _at(page, "boundary", "South by: 30 feet road")]
    cov = full_description_coverage(ex, page)
    assert cov["lots_incomplete"] == {"1": ["boundary"]}
    assert "full_description_incomplete" in _codes(ex)


def test_a_detail_in_another_lots_block_is_a_wrong_lot_not_a_truncation():
    fd2 = "Item 2: vacant site in Plot No 24, Zuzuvadi Village, Hosur Taluk."
    page = FD1 + "\n\n" + fd2
    ex = [_block(page, FD1, lot="1"), _block(page, fd2, lot="2"),
          _at(page, "identifier", "Plot No 24", lot="1", kind="plot")]
    cov = full_description_coverage(ex, page)
    assert cov["lots_incomplete"] == {}
    assert cov["lots_wrong_lot"] == {"1": ["identifier"]}
    report = validate(ex, page)
    assert {i["code"] for i in report["issues"]} >= {"detail_wrong_lot"}
    assert "full_description_incomplete" not in {i["code"] for i in report["issues"]}
    assert report["stats"]["full_description_wrong_lot_lots"] == 1


def test_the_same_fact_worded_differently_is_covered():
    page = "Property Address: D.No. 92/2, Madurai. " + FD1
    ex = [_block(page, FD1), _at(page, "identifier", "D.No. 92/2", kind="door_new")]
    assert full_description_coverage(ex, page)["lots_incomplete"] == {}


def test_a_place_named_in_the_block_is_covered_in_another_order():
    page = FD1 + " Situated at Madurai South Taluk, Ponmeni Village."
    ex = [_block(page, FD1),
          _at(page, "location", "Madurai South Taluk, Ponmeni Village")]
    assert full_description_coverage(ex, page)["lots_incomplete"] == {}


def test_a_weak_number_is_not_matched_on_its_own():
    """"Plot No.5" against a block that says "5 cents": a lone digit is no
    evidence, so the detail stays outside."""
    fd = "All that land measuring 5 cents in Ponmeni Village."
    page = fd + " Also Plot No.5 in the layout."
    ex = [_block(page, fd), _at(page, "identifier", "Plot No.5", kind="plot")]
    assert full_description_coverage(ex, page)["lots_incomplete"] == {"1": ["identifier"]}


def test_a_boundary_is_never_matched_on_numbers():
    """Boundaries name neighbours ("Plot No.14"), whose numbers the block may
    state for other reasons."""
    fd = "Plot No.15 and Plot No.14 in S.No.11, measuring 2795 sq.ft."
    page = fd + " Bounded by West: Plot No.14."
    ex = [_block(page, fd), _at(page, "boundary", "West: Plot No.14")]
    assert full_description_coverage(ex, page)["lots_incomplete"] == {"1": ["boundary"]}


def test_other_spacing_and_an_elided_quote_are_covered():
    page = FD1 + " Door  No. 92 / 2 ;  S.F.No.179/6 ... Madurai South Taluk"
    ex = [_block(page, FD1),
          _at(page, "identifier", "Door  No. 92 / 2", kind="door_new"),
          _at(page, "location", "S.F.No.179/6 ... Madurai South Taluk")]
    assert full_description_coverage(ex, page)["lots_incomplete"] == {}


def test_the_heading_line_and_an_edge_quote_are_covered():
    head = "Item No.1: Residential house site"
    page = head + ": " + FD1
    ex = [_block(page, FD1), _at(page, "property", head),
          E("identifier", page.index(FD1) - 5, page.index(FD1) + 10, text="x")]
    assert full_description_coverage(ex, page)["lots_incomplete"] == {}


def test_records_about_the_property_are_excused_not_scored():
    page = (FD1 + " Property ID: IDIB6622770038. CERSAI ID: 400058772922."
            " Latitude: 12.77495 Longitude: 80.04116. Encumbrance(s): Not Known")
    ex = [_block(page, FD1),
          _at(page, "identifier", "Property ID: IDIB6622770038", kind="property_id"),
          _at(page, "identifier", "400058772922", kind="cersai"),
          _at(page, "location", "Latitude: 12.77495 Longitude: 80.04116"),
          _at(page, "property", "Encumbrance(s): Not Known")]
    cov = full_description_coverage(ex, page)
    assert cov["lots_incomplete"] == {}
    assert cov["lots_excused"] == {"1": {"coordinates": ["location"],
                                         "record_id": ["identifier"],
                                         "status": ["property"]}}
    assert "full_description_incomplete" not in _codes(ex)


def test_the_borrowers_address_is_excused():
    page = ("Borrower: Mr. P. Thanigachalam, residing at No 33, Karunanidhi "
            "Street, Anakaputhur village, Chennai-600070. " + FD1)
    ex = [_block(page, FD1),
          _at(page, "location", "Anakaputhur village, Chennai-600070")]
    cov = full_description_coverage(ex, page)
    assert cov["lots_incomplete"] == {}
    assert cov["lots_excused"] == {"1": {"party_address": ["location"]}}


def test_a_property_named_after_its_owner_is_not_a_borrowers_address():
    """"W/o" or "Borrower" earlier on the line does not make the property
    itself an address when the words right before it say property."""
    page = ("Borrower: Mrs. Subamalini. Details of the Immovable Properties "
            "Mortgaged: Kathattivayal Village, Sivaganga Taluk. " + FD1)
    ex = [_block(page, FD1), _at(page, "location", "Kathattivayal Village, Sivaganga Taluk")]
    assert full_description_coverage(ex, page)["lots_incomplete"] == {"1": ["location"]}


def test_a_repeat_in_the_address_box_or_a_table_is_excused():
    page = ("<table><tr><td>Possession</td><td>UDS-388 Sqft</td></tr></table> "
            "Mortgaged Property Address: Flat No S4, Keelavalavu. " + FD1)
    ex = [_block(page, FD1), _at(page, "extent", "UDS-388 Sqft"),
          _at(page, "location", "Keelavalavu")]
    cov = full_description_coverage(ex, page)
    assert cov["lots_incomplete"] == {}
    assert cov["lots_excused"] == {"1": {"address_box": ["location"],
                                         "table": ["extent"]}}


def test_without_the_page_the_context_tests_are_skipped():
    page = "Mortgaged Property Address: Flat No S4, Keelavalavu. " + FD1
    ex = [_block(page, FD1), _at(page, "location", "Keelavalavu")]
    assert full_description_coverage(ex)["lots_incomplete"] == {"1": ["location"]}
