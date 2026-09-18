"""What an unplaced span costs — the scoring half of the grounding fix.

An entity langextract could not place used to be charged twice: once by
``ungrounded``, and again by ``full_description_incomplete``, because a value
with no span cannot be shown to sit inside its lot's description block. The
second charge is the absence of evidence, not evidence of truncation.

Pure tests — no DB, no LLM.
"""
from __future__ import annotations

from types import SimpleNamespace

from pipeline.validators import _PENALTY, full_description_coverage, validate


def _e(cls, text, span=None, lot="1", **attrs):
    ci = None if span is None else SimpleNamespace(start_pos=span[0],
                                                   end_pos=span[1])
    return SimpleNamespace(extraction_class=cls, extraction_text=text,
                           attributes={"lot_index": lot, **attrs},
                           char_interval=ci)


def codes(report):
    return {i["code"] for i in report["issues"]}


def severity(report, code):
    return next(i["severity"] for i in report["issues"] if i["code"] == code)


# The description block runs 0-60; what follows it does not.
FD = "Land at Kottapattu Village, Trichirapalli Taluk, S.F.No. 197"
PAGE = FD + "\n\nContact: 98400 00000. Earlier sale: Door No. 17."


# ── coverage: outside the block vs. not placeable at all ────────────────────

def test_a_placed_span_outside_the_block_is_still_incomplete():
    """The real finding this check exists for: full_description was truncated
    before a detail that sits elsewhere on the page."""
    cov = full_description_coverage([
        _e("full_description", FD, (0, 60)),
        _e("identifier", "Door No. 17", (95, 106)),
    ])
    assert cov["lots_incomplete"] == {"1": ["identifier"]}
    assert cov["lots_unverifiable"] == {}


def test_an_unplaced_entity_is_unverifiable_not_incomplete():
    cov = full_description_coverage([
        _e("full_description", FD, (0, 60)),
        _e("identifier", "Door No. 17", None),
    ])
    assert cov["lots_incomplete"] == {}
    assert cov["lots_unverifiable"] == {"1": ["identifier"]}


def test_an_unplaced_full_description_cannot_condemn_its_own_lot():
    """Containment needs two spans. If the block itself was never placed, no
    entity can be shown to fall outside it."""
    cov = full_description_coverage([
        _e("full_description", FD, None),
        _e("identifier", "Door No. 17", (95, 106)),
    ])
    assert cov["lots_incomplete"] == {}
    assert cov["lots_unverifiable"] == {"1": ["identifier"]}


def test_text_inside_the_block_is_covered_however_it_was_placed():
    """The text arm is unchanged: a value repeated inside the block's own text
    is derivable from it, span or no span."""
    cov = full_description_coverage([
        _e("full_description", FD, (0, 60)),
        _e("location", "Kottapattu Village", None),
    ])
    assert cov["lots_incomplete"] == {} and cov["lots_unverifiable"] == {}


def test_the_missing_span_is_charged_once_not_twice():
    ents = [_e("full_description", FD, (0, 60)),
            _e("identifier", "Door No. 17", None)]
    report = validate(ents, source_text=PAGE)
    assert "ungrounded" in codes(report)
    assert "full_description_incomplete" not in codes(report)


# ── how much of the notice lost its anchor decides what it costs ────────────

def test_one_stray_entity_is_a_blemish():
    ents = [_e("full_description", FD, (0, 60))]
    ents += [_e("identifier", f"S.F.No. {i}", (0, 10)) for i in range(40)]
    ents.append(_e("location", "composed value", None))      # 1 of 42
    report = validate(ents, source_text=PAGE)
    assert severity(report, "ungrounded") == "low"


def test_a_page_the_model_stopped_quoting_is_charged_high():
    ents = [_e("full_description", FD, (0, 60))]
    ents += [_e("identifier", f"S.F.No. {i}", None) for i in range(9)]
    report = validate(ents, source_text=PAGE)                 # 9 of 10
    assert severity(report, "ungrounded") == "high"
    assert _PENALTY["high"] > _PENALTY["low"]


def test_the_middle_keeps_the_old_weight():
    ents = [_e("full_description", FD, (0, 60))]
    ents += [_e("identifier", f"S.F.No. {i}", (0, 10)) for i in range(8)]
    ents += [_e("location", f"composed {i}", None) for i in range(1)]  # 1 of 10
    report = validate(ents, source_text=PAGE)
    assert severity(report, "ungrounded") == "med"


def test_a_fully_grounded_notice_is_not_flagged_at_all():
    ents = [_e("full_description", FD, (0, 60)),
            _e("location", "Kottapattu Village", (8, 26))]
    assert "ungrounded" not in codes(validate(ents, source_text=PAGE))
