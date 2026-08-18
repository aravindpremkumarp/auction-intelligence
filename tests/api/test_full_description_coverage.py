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


# ── order-insensitive (token) coverage arm ───────────────────────────────────
#
# The extractor frequently SYNTHESISES an entity instead of copying it, emitting
# a location in canonical order while the notice states the same facts in a
# different order. Those entities are ungrounded (no span) and never match as a
# substring, so before the token arm they read as "full_description truncated"
# on notices whose description was complete.

_FD = ("All that piece and parcel of the land and building in Tiruvannamalai "
       "District, Tiruvannamalai Registration District, Chengam Taluk, Chengam "
       "Sub Registration District, Thokkavadi Village, Patta No.44")


def test_reordered_location_is_covered():
    # every token is present in fd, only the ORDER differs -> not truncation.
    ex = [E("full_description", 100, 300, text=_FD),
          E("location", None, None, text="Thokkavadi Village, Chengam Taluk, "
                                         "Tiruvannamalai District")]
    assert full_description_coverage(ex)["lots_incomplete"] == {}


def test_inferred_state_does_not_count_as_truncation():
    # "Tamil Nadu" is derivable from the district and the notice never spells it
    # out; the state attribute exempts exactly those tokens.
    ex = [E("full_description", 100, 300, text=_FD),
          E("location", None, None,
            text="Thokkavadi Village, Chengam Taluk, Tiruvannamalai District, "
                 "Tamil Nadu",
            state="Tamil Nadu")]
    assert full_description_coverage(ex)["lots_incomplete"] == {}


def test_token_arm_still_detects_real_truncation():
    # a genuinely truncated description loses whole values, not just their
    # order — the token arm must NOT forgive that.
    ex = [E("full_description", 100, 300, text=_FD),
          E("boundary", None, None,
            text="East: Road, West: Plot No.9, North: Canal, South: Masuthi")]
    assert full_description_coverage(ex)["lots_incomplete"] == {"1": ["boundary"]}


def test_unrelated_state_attribute_does_not_whitewash_missing_detail():
    # the exemption is scoped to the state's OWN tokens; other missing content
    # still flags.
    ex = [E("full_description", 100, 300, text=_FD),
          E("location", None, None, text="Kanchipuram District, Tamil Nadu",
            state="Tamil Nadu")]
    assert full_description_coverage(ex)["lots_incomplete"] == {"1": ["location"]}


def test_integer_lot_index_does_not_crash_sorting():
    # lot_index arrives as an int on some extractions; mixing int and str lots
    # used to raise TypeError inside sorted().
    ex = [E("property", 100, 180, lot=1), E("location", 200, 240, lot="2")]
    cov = full_description_coverage(ex)
    assert cov["lots_missing_full_description"] == ["1", "2"]
    assert "missing_full_description" in _codes(ex)
