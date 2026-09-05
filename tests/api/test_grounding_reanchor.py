"""Re-anchoring LangExtract grounding after the markdown changes (api/review/grounding.py).

The failure this exists for: `pipeline/load_extractions.py` stores each
entity's char span against the exact markdown langextract ran on. A re-ingest
rewrites that string, every offset after the edit shifts, and nothing errors —
the extraction UI just maps each field onto whatever text now sits there while
`grounded` keeps reporting True.

Pure-function, DB-free, in the idiom of tests/api/test_markdown_match.py.
"""
from __future__ import annotations

import json

from api.review.grounding import (ANCHOR_FUZZY, ANCHOR_LOST, ANCHOR_NONE,
                                  ANCHOR_RELOCATED, ANCHOR_STORED,
                                  ANCHOR_UNVERIFIED, FUZZY_MIN_CHARS,
                                  anchor_entity, reanchor)

# A notice fragment shaped like the real ones: a header the re-ingest will
# lengthen, then the values an extraction grounds on.
HEAD = "# E-AUCTION SALE NOTICE\n\n"
BODY = ("Borrower: Aravinth S. Reserve Price: Rs.24,48,217/- for the property "
        "at Pichadarkovil Village, Manachanallur Taluk.")
MD = HEAD + BODY


def _ent(text, start=None, end=None, **kw):
    if start is None:
        start = MD.find(text)
        end = start + len(text)
    return {"id": kw.get("id", "0"), "cls": kw.get("cls", "auction_terms"),
            "text": text, "start": start, "end": end, "attrs": kw.get("attrs", {})}


# ── the common case: nothing moved ──────────────────────────────────────────

def test_unchanged_markdown_verifies_every_span():
    ents = [_ent("Aravinth S"), _ent("Rs.24,48,217/-"), _ent("Manachanallur")]
    out, summary = reanchor(ents, MD)
    assert [e["anchor"] for e in out] == [ANCHOR_STORED] * 3
    assert summary["moved"] == 0
    for e in out:
        assert MD[e["start"]:e["end"]] == e["text"]


def test_input_is_never_mutated():
    ents = [_ent("Rs.24,48,217/-", start=0, end=5)]
    before = json.dumps(ents, sort_keys=True)
    reanchor(ents, MD)
    assert json.dumps(ents, sort_keys=True) == before


# ── the regression: a re-ingest shifted the text ────────────────────────────

def test_shifted_markdown_relocates_the_span():
    # A re-ingest that reads the header the notice actually has pushes every
    # later offset along. The stored span now lands mid-sentence.
    ents = [_ent("Rs.24,48,217/-")]
    shifted = "# PUBLIC NOTICE FOR E-AUCTION CUM SALE UNDER SARFAESI\n\n" + BODY
    stale = ents[0]["start"]
    assert shifted[stale:stale + 14] != "Rs.24,48,217/-"      # the bug, verbatim
    out, summary = reanchor(ents, shifted, markdown_changed=True)
    assert out[0]["anchor"] == ANCHOR_RELOCATED
    assert shifted[out[0]["start"]:out[0]["end"]] == "Rs.24,48,217/-"
    assert summary["moved"] == 1


# ── a stored span is not a quotation ────────────────────────────────────────
# langextract normalizes as it extracts (a newline inside a phone number
# collapsed to a space, a space inserted after a comma) and sometimes composes
# a value that was never contiguous. Only 73% of live spans are exact slices,
# so equality is the wrong test and "not equal" is not a broken anchor.

def test_normalized_text_still_verifies_as_stored():
    md = HEAD + "Tel.No. 044 - 2849 6339\n\nE-MAIL:cb2361@canarabank.com yours"
    # Verbatim from the corpus: the entity collapsed the blank line to a space.
    text = "Tel.No. 044 - 2849 6339 E-MAIL:cb2361@canarabank.com"
    start = md.find("Tel.No.")
    end = start + len("Tel.No. 044 - 2849 6339\n\nE-MAIL:cb2361@canarabank.com")
    out, _ = reanchor([_ent(text, start, end)], md)
    assert out[0]["anchor"] == ANCHOR_STORED
    assert (out[0]["start"], out[0]["end"]) == (start, end)


def test_a_near_miss_span_still_verifies():
    """Punctuation and spacing differences are what langextract does to a
    passage it did read — the span is right and must not be second-guessed."""
    md = HEAD + "Tirupur Registration District - Thottipalayam Sub Registration"
    text = "Tirupur Registration District, Thottipalayam Sub Registration"
    start = md.find("Tirupur")
    out, _ = reanchor([_ent(text, start, start + len(text))], md)
    assert out[0]["anchor"] == ANCHOR_STORED


def test_a_composed_value_keeps_its_span_while_the_markdown_stands():
    """The value was assembled from opposite corners of a table, so it is
    nowhere in the text verbatim and the stored span covers only part of it.
    langextract's alignment is approximate — and the only reading of the page
    that exists, so it must not be thrown away."""
    md = (HEAD + "| Reserve Price Rs.75,33,000/- | col | col | col |\n"
          "| lots of unrelated cells between them |\n| EMD Rs.7,53,300/- |")
    text = "Reserve Price Rs.75,33,000/-, EMD Rs.7,53,300/-, Bid Increment Rs.75,000/-"
    start, end = md.find("Reserve"), md.find("Reserve") + 28
    out, _ = reanchor([_ent(text, start, end)], md)
    assert out[0]["anchor"] == ANCHOR_UNVERIFIED
    assert (out[0]["start"], out[0]["end"]) == (start, end)   # kept, not guessed


def test_the_same_composed_value_is_dropped_once_the_markdown_is_rewritten():
    """Same entity, but the text it was aligned against no longer exists. Now
    the span points at nothing real, so keeping it would be inventing evidence."""
    text = "Reserve Price Rs.75,33,000/-, EMD Rs.7,53,300/-, Bid Increment Rs.75,000/-"
    out, _ = reanchor([_ent(text, 0, 60)], "a completely different notice body",
                      markdown_changed=True)
    assert out[0]["anchor"] == ANCHOR_LOST
    assert out[0]["start"] is None


def test_a_stale_span_that_happens_to_land_on_other_text_is_not_trusted():
    # The dangerous shape: the old offsets are in range and point at real
    # words, just the wrong ones. Verification is by content, not bounds.
    ents = [_ent("Aravinth S", start=MD.find("Manachanallur"),
                 end=MD.find("Manachanallur") + 10)]
    out, _ = reanchor(ents, MD)
    assert out[0]["anchor"] == ANCHOR_RELOCATED
    assert MD[out[0]["start"]:out[0]["end"]] == "Aravinth S"


# ── repeated values re-anchor in document order ─────────────────────────────

def test_repeated_values_keep_their_order_instead_of_collapsing():
    """A multi-lot notice repeats the same figure once per lot. Without the
    cursor every one of them re-anchors onto the first occurrence."""
    md = ("Lot 1 borrower Arun reserve Rs.5,00,000 EMD paid. "
          "Lot 2 borrower Bala reserve Rs.5,00,000 EMD paid. "
          "Lot 3 borrower Chitra reserve Rs.5,00,000 EMD paid.")
    ents = [{"id": str(i), "cls": "auction_terms", "text": "Rs.5,00,000",
             "start": 9999, "end": 10010, "attrs": {}} for i in range(3)]
    out, _ = reanchor(ents, md)
    starts = [e["start"] for e in out]
    assert starts == sorted(starts) and len(set(starts)) == 3
    for e in out:
        assert md[e["start"]:e["end"]] == "Rs.5,00,000"


def test_cursor_wraps_when_an_entity_appears_earlier_in_the_page():
    # Entities are usually in document order, but not guaranteed. A value that
    # only exists before the cursor must still be found.
    md = "alpha Rs.1,00,000 beta Rs.2,00,000 gamma"
    ents = [_ent("Rs.2,00,000", 0, 11), _ent("Rs.1,00,000", 0, 11)]
    out, _ = reanchor(ents, md)
    assert [md[e["start"]:e["end"]] for e in out] == ["Rs.2,00,000", "Rs.1,00,000"]


# ── fuzzy: the OCR changed the characters themselves ────────────────────────

def test_ocr_drift_matches_fuzzily():
    # A re-ingest that BOTH moved the text and respelled the village: the old
    # span is worthless and the value is not findable verbatim, so similarity
    # is the best reading left.
    ents = [_ent("Pichadarkovil Village")]
    md = ("# PUBLIC NOTICE FOR E-AUCTION CUM SALE UNDER SARFAESI ACT 2002\n\n"
          + BODY.replace("Pichadarkovil", "Pichadarkovill"))
    out, _ = reanchor(ents, md, markdown_changed=True)
    assert out[0]["anchor"] == ANCHOR_FUZZY
    assert "Pichadarkovil" in md[out[0]["start"]:out[0]["end"]]


def test_fuzzy_is_not_attempted_while_the_markdown_stands():
    """Replacing langextract's approximate span with a speculative one is not
    an improvement — fuzzy is only worth the risk once the span is worthless."""
    md = ("# PUBLIC NOTICE FOR E-AUCTION CUM SALE UNDER SARFAESI ACT 2002\n\n"
          + BODY.replace("Pichadarkovil", "Pichadarkovill"))
    out, _ = reanchor([_ent("Pichadarkovil Village", 0, 20)], md)
    assert out[0]["anchor"] == ANCHOR_UNVERIFIED
    assert (out[0]["start"], out[0]["end"]) == (0, 20)


def test_a_short_value_is_lost_rather_than_fuzzily_guessed():
    # "No.9" is inside a dozen unrelated strings; a confident wrong anchor is
    # worse than none, so anything under FUZZY_MIN_CHARS must not fuzzy-match.
    md = "Plot No.51, Survey No.52 and Door No.53 of the layout."
    assert len("No.5") < FUZZY_MIN_CHARS
    out, _ = reanchor([{"id": "0", "cls": "identifier", "text": "No.5",
                        "start": 900, "end": 904, "attrs": {}}], md,
                      markdown_changed=True)
    # Findable verbatim here, so it relocates — the guard is that a short
    # value never reaches the fuzzy stage.
    assert out[0]["anchor"] == ANCHOR_RELOCATED
    out, _ = reanchor([{"id": "0", "cls": "identifier", "text": "No.9",
                        "start": 900, "end": 904, "attrs": {}}], md,
                      markdown_changed=True)
    assert out[0]["anchor"] == ANCHOR_LOST
    assert out[0]["start"] is None


def test_text_that_is_simply_gone_is_dropped_not_guessed():
    ents = [_ent("Rs.24,48,217/-")]
    out, summary = reanchor(ents, HEAD + "The property was withdrawn from sale.",
                            markdown_changed=True)
    assert out[0]["anchor"] == ANCHOR_LOST
    assert out[0]["start"] is None and out[0]["end"] is None
    assert summary[ANCHOR_LOST] == 1


def test_value_and_attributes_survive_a_lost_anchor():
    ents = [_ent("Rs.24,48,217/-", attrs={"reserve_price_num": "2448217"})]
    out, _ = reanchor(ents, "nothing like it here", markdown_changed=True)
    assert out[0]["text"] == "Rs.24,48,217/-"
    assert out[0]["attrs"] == {"reserve_price_num": "2448217"}


# ── absence of evidence ─────────────────────────────────────────────────────

def test_no_markdown_leaves_stored_spans_exactly_as_they_are():
    """Nothing to verify against is not proof the span is wrong."""
    ents = [_ent("Rs.24,48,217/-"), {"id": "1", "cls": "location",
                                     "text": "X", "start": None,
                                     "end": None, "attrs": {}}]
    for md in (None, ""):
        out, _ = reanchor(ents, md)
        assert (out[0]["start"], out[0]["end"]) == (ents[0]["start"], ents[0]["end"])
        assert out[0]["anchor"] == ANCHOR_STORED
        assert out[1]["anchor"] == ANCHOR_NONE


def test_ungrounded_entity_stays_ungrounded():
    out, summary = reanchor([{"id": "0", "cls": "location", "text": "",
                              "start": None, "end": None, "attrs": {}}], MD)
    assert out[0]["anchor"] == ANCHOR_NONE
    assert summary["checked"] == 1


def test_summary_counts_what_happened():
    ents = [_ent("Aravinth S"),                                  # stored
            _ent("Rs.24,48,217/-", start=0, end=14),             # relocated
            _ent("Rs.99,99,999/-", start=0, end=14)]             # lost
    _, summary = reanchor(ents, MD, markdown_changed=True)
    assert summary["checked"] == 3
    assert summary[ANCHOR_STORED] == 1
    assert summary[ANCHOR_RELOCATED] == 1
    assert summary[ANCHOR_LOST] == 1
    assert summary["moved"] == 2


def test_non_dict_entries_are_skipped():
    out, summary = reanchor(["junk", None, _ent("Aravinth S")], MD)
    assert summary["checked"] == 1 and out[0]["anchor"] == ANCHOR_STORED


def test_anchor_entity_is_usable_on_its_own():
    start, end, status = anchor_entity(MD, "Aravinth S", 0, 10)
    assert status == ANCHOR_RELOCATED
    assert MD[start:end] == "Aravinth S"


# ── wiring: the review field reflects the re-anchored span ──────────────────

def test_build_fields_reanchors_and_reports_the_status():
    from api.review.extraction import _build_fields
    shifted = "# PUBLIC NOTICE FOR E-AUCTION CUM SALE UNDER SARFAESI\n\n" + BODY
    ej = json.dumps([_ent("Rs.24,48,217/-"), _ent("Aravinth S")])
    fields = _build_fields(ej, "{}", shifted, True)
    assert all(f.grounded for f in fields)
    assert {f.anchor for f in fields} == {ANCHOR_RELOCATED}
    for f in fields:
        assert shifted[f.start:f.end] == f.text


def test_build_fields_marks_a_lost_field_ungrounded():
    from api.review.extraction import _build_fields
    ej = json.dumps([_ent("Rs.24,48,217/-")])
    (f,) = _build_fields(ej, "{}", "the notice was withdrawn", True)
    assert f.grounded is False
    assert f.anchor == ANCHOR_LOST
    assert f.text == "Rs.24,48,217/-"        # the value is never lost


def test_build_fields_defaults_to_keeping_spans_when_not_stale():
    """The default path (markdown present, not stale) must never drop a span —
    that is the healthy case, and it is where nearly every document sits."""
    from api.review.extraction import _build_fields
    ej = json.dumps([_ent("Rs.24,48,217/-", start=0, end=14)])
    (f,) = _build_fields(ej, "{}", MD)
    assert f.grounded is True
    assert f.anchor in (ANCHOR_RELOCATED, ANCHOR_UNVERIFIED)


def test_build_fields_without_markdown_keeps_todays_behaviour():
    from api.review.extraction import _build_fields
    ej = json.dumps([_ent("Rs.24,48,217/-"),
                     {"id": "1", "cls": "location", "text": "X",
                      "start": None, "end": None, "attrs": {}}])
    grounded, ungrounded = _build_fields(ej, "{}")
    assert grounded.grounded is True and grounded.anchor == ANCHOR_STORED
    assert ungrounded.grounded is False and ungrounded.anchor == ANCHOR_NONE
