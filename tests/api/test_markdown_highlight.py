"""Unit tests for markdown highlight spans (the review-UI match locator)."""
from __future__ import annotations

from api.review.markdown_match import match_span
from api.review.queries import _attach_markdown_highlights


# A multi-property notice: three lots, only some tracked in the DB.
MARKDOWN = (
    "E-AUCTION SALE NOTICE\n\n"
    "| No. | Particulars | Price |\n"
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


def test_match_span_locates_the_right_block():
    # Scraped description carries OCR/scraper noise + the field bleed.
    desc = ("Flat No. 1H, Block 1, First Floor, Aswini Amanya, Nellikuppam "
            "Village Chengalpattu Taluk. Kancheepuram District Tamil "
            "Nadu-603108 [1061 sq. ft. super built-up area]"
            "Province/State :Tamil NaduCity/Town :Kancheepuram")
    span = match_span(desc, MARKDOWN)
    assert span is not None
    start, end = span
    found = MARKDOWN[start:end]
    # It must land on the 1H / 1061 lot, not the 1G or 2G ones.
    assert "1H" in found and "1061" in found
    assert "Flat No. 1G" not in found and "Flat No. 2G" not in found


def test_match_span_distinct_blocks_for_distinct_properties():
    d1 = "Flat No. 1G, Block 1, First Floor [1062 sq. ft. super built-up area]"
    d2 = "Flat No. 2G, Block 2, Second Floor [1062 sq. ft. super built-up area]"
    s1, s2 = match_span(d1, MARKDOWN), match_span(d2, MARKDOWN)
    assert s1 is not None and s2 is not None and s1 != s2


def test_match_span_none_for_absent_or_empty():
    assert match_span("Some totally unrelated industrial shed in Gujarat", MARKDOWN) is None
    assert match_span("", MARKDOWN) is None
    assert match_span("Flat No. 1H", "") is None
    assert match_span(None, MARKDOWN) is None


def test_attach_highlights_builds_spans_and_drops_descriptions():
    rows = [{
        "markdown": MARKDOWN,
        "website_descriptions": [
            "Flat No. 1H, Block 1, First Floor [1061 sq. ft. super built-up area]",
            "Flat No. 2G, Block 2, Second Floor [1062 sq. ft. super built-up area]",
            None,  # a property with no scraped description
        ],
    }]
    _attach_markdown_highlights(rows)
    row = rows[0]
    assert "website_descriptions" not in row          # not leaked to the client
    assert len(row["highlights"]) == 2                 # two matched, None skipped
    for h in row["highlights"]:
        assert 0 <= h["start"] < h["end"] <= len(MARKDOWN)
    # spans are sorted by start offset
    assert row["highlights"] == sorted(row["highlights"], key=lambda h: h["start"])


def test_attach_highlights_empty_when_no_markdown():
    rows = [{"markdown": "", "website_descriptions": ["Flat No. 1H"]}]
    _attach_markdown_highlights(rows)
    assert rows[0]["highlights"] == []
    assert "website_descriptions" not in rows[0]
