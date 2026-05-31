"""Unit tests for markdown highlight spans (the review-UI match locator)."""
from __future__ import annotations

from api.review.markdown_match import match_span
from api.review.queries import (
    _attach_markdown_highlights,
    _attach_property_highlight,
)


# A multi-property notice: three lots in one GFM table (one row per line, as
# the OCR renders it), only some of which are tracked in the DB.
MARKDOWN = (
    "E-AUCTION SALE NOTICE\n\n"
    "| No. | Particulars | Price |\n"
    "| --- | --- | --- |\n"
    "| 1 | Flat No. 1G, Block 1, First Floor, Aswini Amanya, Nellikuppam "
    "Village Chengalpattu Taluk, Kancheepuram District Tamil Nadu -603108 "
    "[1062 sq. ft. super built-up area] | 26.98 |\n"
    "| 2 | Flat No. 1H, Block 1, First Floor, Aswini Amanya, Nellikuppam "
    "Village Chengalpattu Taluk, Kancheepuram District Tamil Nadu -603108 "
    "[1061 sq. ft. super built-up area] | 26.96 |\n"
    "| 3 | Flat No. 2G, Block 2, Second Floor, Aswini Amanya, Nellikuppam "
    "Village Chengalpattu Taluk, Kancheepuram District Tamil Nadu -603108 "
    "[1062 sq. ft. super built-up area] | 26.98 |\n"
)

# Scraped descriptions as they actually appear: near-verbatim copies of the
# block, with minor OCR/scraper noise and (for 1H) the field bleed appended.
DESC_1G = ("Flat No. 1G, Block 1, First Floor, Aswini Amanya, Nellikuppam "
           "Village Chengalpattu Taluk. Kancheepuram District Tamil Nadu-603108 "
           "[1062 sq. ft. super built-up area]")
DESC_1H = ("Flat No. 1H, Block 1, First Floor, Aswini Amanya, Nellikuppam "
           "Village Chengalpattu Taluk. Kancheepuram District Tamil Nadu-603108 "
           "[1061 sq. ft. super built-up area]"
           "Province/State :Tamil NaduCity/Town :Kancheepuram")
DESC_2G = ("Flat No. 2G, Block 2, Second Floor, Aswini Amanya, Nellikuppam "
           "Village Chengalpattu Taluk. Kancheepuram District Tamil Nadu-603108 "
           "[1062 sq. ft. super built-up area]")


def test_match_span_locates_the_right_block():
    span = match_span(DESC_1H, MARKDOWN)
    assert span is not None
    found = MARKDOWN[span[0]:span[1]]
    # It must land on the 1H / 1061 lot, not the 1G or 2G ones.
    assert "1H" in found and "1061" in found
    assert "Flat No. 1G" not in found and "Flat No. 2G" not in found


def test_match_span_distinct_blocks_for_distinct_properties():
    s1, s2 = match_span(DESC_1G, MARKDOWN), match_span(DESC_2G, MARKDOWN)
    assert s1 is not None and s2 is not None and s1 != s2


def test_match_span_none_for_absent_or_empty():
    assert match_span("Some totally unrelated industrial shed in Gujarat", MARKDOWN) is None
    assert match_span("", MARKDOWN) is None
    assert match_span(DESC_1H, "") is None
    assert match_span(None, MARKDOWN) is None


def test_attach_highlights_builds_spans_and_drops_descriptions():
    rows = [{
        "markdown": MARKDOWN,
        "website_descriptions": [DESC_1H, DESC_2G, None],  # None = property w/o scraped desc
    }]
    _attach_markdown_highlights(rows)
    row = rows[0]
    assert "website_descriptions" not in row          # not leaked to the client
    assert len(row["highlights"]) == 2                 # two matched, None skipped
    for h in row["highlights"]:
        assert 0 <= h["start"] < h["end"] <= len(MARKDOWN)
    assert row["highlights"] == sorted(row["highlights"], key=lambda h: h["start"])


def test_attach_highlights_empty_when_no_markdown():
    rows = [{"markdown": "", "website_descriptions": [DESC_1H]}]
    _attach_markdown_highlights(rows)
    assert rows[0]["highlights"] == []
    assert "website_descriptions" not in rows[0]


# ── property-detail single highlight (the enrichment view) ──────────────────

def test_attach_property_highlight_single_doc():
    row = {
        "website_description": DESC_1H,
        "description": None,
        "documents": [{"markdown": MARKDOWN}],
    }
    _attach_property_highlight(row)
    h = row["markdown_highlight"]
    assert h is not None and h["doc_index"] == 0
    found = MARKDOWN[h["start"]:h["end"]]
    assert "1H" in found and "1061" in found


def test_attach_property_highlight_picks_best_of_many_docs():
    # The property links to two notices; only the second contains its block.
    other = "Some unrelated industrial shed auction in a different state entirely."
    row = {
        "website_description": DESC_2G,
        "documents": [{"markdown": other}, {"markdown": MARKDOWN}],
    }
    _attach_property_highlight(row)
    h = row["markdown_highlight"]
    assert h is not None and h["doc_index"] == 1
    assert "2G" in MARKDOWN[h["start"]:h["end"]]


def test_attach_property_highlight_none_when_no_match_or_no_markdown():
    row = {"website_description": "totally unrelated text", "documents": [{"markdown": MARKDOWN}]}
    _attach_property_highlight(row)
    assert row["markdown_highlight"] is None

    row2 = {"website_description": DESC_1H, "documents": [{"markdown": ""}]}
    _attach_property_highlight(row2)
    assert row2["markdown_highlight"] is None

    row3 = {"website_description": "", "description": "", "documents": []}
    _attach_property_highlight(row3)
    assert row3["markdown_highlight"] is None
