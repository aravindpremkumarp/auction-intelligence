"""Intrinsic OCR-health scoring (pipeline/ocr_health.py).

Pure-function, DB-free. Fixtures mirror the real MinerU failure modes
observed on full-page ruled notices (repetition loops, control-token
leakage, truncated tables) and the legitimate look-alikes that must NOT
flag (per-lot boilerplate repeated *non*-consecutively).
"""
from __future__ import annotations

from pipeline.ocr_health import score_block_health, score_ocr_health


def test_clean_prose_scores_100():
    md = ("# SALE NOTICE FOR SALE OF IMMOVABLE PROPERTIES\n\n"
          "Notice is hereby given to the public in general and in "
          "particular to the Borrower(s) that the below described "
          "immovable property will be sold on \"As is where is\" basis.")
    h = score_ocr_health(md)
    assert h["score"] == 100
    assert h["flags"] == []


def test_interleaved_boilerplate_does_not_flag():
    # Real notices repeat "For details and queries…" once per lot — but
    # interleaved with distinct rows. Only consecutive runs are loops.
    rows = []
    for i in range(10):
        rows.append(f"Property {i}: land at survey no {i} extent {100 + i} sq.ft")
        rows.append("For details and queries on purchase contact 8190005434")
    h = score_ocr_health("\n".join(rows))
    assert h["flags"] == []
    assert h["score"] == 100


def test_consecutive_line_loop_flags_repetition():
    md = "intro line\n" + ("Registration Distribution\n" * 30) + "outro"
    h = score_ocr_health(md)
    assert "repetition" in h["flags"]
    assert h["details"]["repetition_run"] >= 30
    assert h["score"] < 100


def test_short_run_below_threshold_does_not_flag():
    md = "a real paragraph\n" + ("Rs.10,000/-\n" * 4) + "another paragraph"
    assert score_ocr_health(md)["flags"] == []


def test_inline_phrase_loop_flags_repetition():
    # Single-line HTML table (no newlines) with an adjacent phrase loop.
    md = "<table><tr><td>" + ("Registration District 2 Sub, " * 12) + "</td></tr></table>"
    h = score_ocr_health(md)
    assert "repetition" in h["flags"]
    assert "repetition_inline" in h["details"]


def test_token_leak_raw():
    h = score_ocr_health("some text <|content_end|> more text")
    assert "token-leak" in h["flags"]


def test_token_leak_html_escaped():
    # MinerU escapes the leaked token inside table cells.
    h = score_ocr_health("<table><tr><td>x&lt;|content_end|&gt;</td></tr></table>")
    assert "token-leak" in h["flags"]


def test_unclosed_table_flags_truncated():
    h = score_ocr_health("<table><tr><td>row</td></tr>")
    assert "truncated" in h["flags"]
    assert h["details"]["truncation"] == "unclosed-table"


def test_empty_tail_cells_flag_truncated():
    md = ("<table><tr><td>ok</td><td>ok</td></tr>"
          "<tr><td>partial</td><td></td><td></td></tr></table>")
    h = score_ocr_health(md)
    assert "truncated" in h["flags"]
    assert h["details"]["truncation"] == "empty-tail-cells"


def test_ends_mid_tag_flags_truncated():
    h = score_ocr_health("<table><tr><td>text</td></tr></table>\n<td")
    assert "truncated" in h["flags"]


def test_healthy_html_table_not_truncated():
    md = ("<table><tr><td>a</td><td>b</td></tr>"
          "<tr><td>c</td><td>d</td></tr></table>")
    h = score_ocr_health(md)
    assert h["flags"] == []
    assert h["score"] == 100


def test_composite_failure_scores_zero():
    # Crop-D-shaped output: loop + leak + padded empty tail.
    md = ("<table><tr><td>1.</td><td>borrower</td></tr><tr><td>"
          + ("Rs.10,000/-\n" * 20)
          + "&lt;|content_end|&gt;</td><td></td><td></td></tr></table>")
    h = score_ocr_health(md)
    assert set(h["flags"]) == {"repetition", "token-leak", "truncated"}
    assert h["score"] == 0


def test_empty_and_none_are_unscored():
    assert score_ocr_health("") == {"score": None, "flags": [], "details": {}}
    assert score_ocr_health(None) == {"score": None, "flags": [], "details": {}}
    assert score_ocr_health("   \n ")["score"] is None


def test_cjk_hallucination_flags_foreign_script():
    # Real leak observed in prod: Chinese bank boilerplate inside a TN notice.
    md = "All that piece and parcel of land 年月日中国银行股份有 at Theni District"
    h = score_ocr_health(md)
    assert "foreign-script" in h["flags"]
    assert h["details"]["foreign_script_count"] == 10
    assert h["details"]["foreign_script_sample"].startswith("年月日")


def test_single_stray_cjk_char_flags():
    h = score_ocr_health("Reserve price Rs.10,71,000 六 EMD Rs.1,07,100")
    assert "foreign-script" in h["flags"]


def test_tamil_and_devanagari_do_not_flag():
    md = ("தமிழ்நாடு தேனி மாவட்டம் — சொத்து விவரம்\n"
          "संपत्ति का विवरण\n"
          "All the piece and parcel of land at Periyakulam Registration District")
    h = score_ocr_health(md)
    assert h["flags"] == []
    assert h["score"] == 100


def test_latin_punctuation_and_symbols_do_not_flag():
    md = "Rs.10,71,000/- @ 12% p.a. — “as is where is” • ₹ 1,07,100"
    assert score_ocr_health(md)["flags"] == []


def test_single_table_collapse_flags():
    # The Motilal-Oswal failure mode: MinerU vlm read a fully-bordered notice
    # as ONE big table, swallowing every prose paragraph into cells. Nothing
    # survives outside the grid.
    cells = (
        "Motilal Oswal Home Finance Limited PUBLIC NOTICE FOR E-AUCTION CUM SALE. "
        "E-Auction Sale Notice of 30 Days for Sale of Immovable Assets under the "
        "Securitisation and Reconstruction of Financial Assets and Enforcement of "
        "Security Interest Act 2002 read with provision to rule 8 and 9. Notice is "
        "hereby given to the public and to the borrowers that the property mortgaged "
        "to Motilal Oswal Home Finance Limited (earlier known as Aspire Home Finance "
        "Corporation Limited) will be sold on As is where is, As is what is, and "
        "Whatever there is basis for recovery of dues and further interest, charges "
        "and costs. LAN LXMOTRICHY5424-250797933 Branch Trichy Borrower Aravinth S "
        "Co-Borrower Tamilarasi Sakkarathan Reserve Price Rs.2448217 EMD Rs.244822 "
        "Description Flat No 03 Block No A Ground Floor Area 740 Sq Feet At Iswar "
        "Builder A Block Pichadarkovil Village Manachanallur Taluk Tiruchirappalli "
        "District. Contact Rajasekaran 7045501738 and Arumugakumar 9677997577."
    )
    md = f"<table><tr><td>{cells}</td></tr></table>"
    h = score_ocr_health(md)
    assert "table-collapse" in h["flags"]
    assert h["details"]["table_collapse"]["outside_chars"] == 0
    assert h["score"] < 100


def test_table_with_prose_outside_does_not_collapse_flag():
    # Faithfully decomposed notice: substantial prose OUTSIDE the grid, a large
    # auction-details table too. The prose keeps the outside-text share well
    # above the collapse ceiling, so it must not flag.
    prose = (
        "# Motilal Oswal Home Finance Limited\n\n"
        "E-Auction Sale Notice of 30 Days for Sale of Immovable Assets under the "
        "Securitisation and Reconstruction of Financial Assets and Enforcement of "
        "Security Interest Act 2002 read with provision to rule 8 and 9 of the "
        "Security Interest (Enforcement) Rules 2002. Notice is hereby given to the "
        "public in general and to the borrowers that the property mortgaged to "
        "Motilal Oswal Home Finance Limited will be sold on As is where is basis "
        "for recovery of dues and further interest, charges and costs. The auction "
        "is conducted as per the terms and conditions of the bid document.\n\n"
    )
    rows = "".join(
        f"<tr><td>Lot {i} borrower name and survey number {i} extent {i}00 "
        f"sq ft village Manachanallur taluk Tiruchirappalli district</td></tr>"
        for i in range(12)
    )
    md = prose + f"<table>{rows}</table>"
    h = score_ocr_health(md)
    assert "table-collapse" not in h["flags"]


def test_multiple_tables_do_not_collapse_flag():
    # Two tables means MinerU preserved layout structure — not a collapse.
    big = "borrower name survey number extent village taluk district " * 12
    md = (f"<table><tr><td>{big}</td></tr></table>\n\n"
          f"<table><tr><td>{big}</td></tr></table>")
    h = score_ocr_health(md)
    assert "table-collapse" not in h["flags"]


def test_small_table_does_not_collapse_flag():
    # A short legitimate grid (below the min-table-size gate) never flags,
    # even with no prose around it.
    md = ("<table><tr><td>LAN</td><td>Reserve Price</td></tr>"
          "<tr><td>LXMOTRICHY5424</td><td>Rs.2448217</td></tr></table>")
    h = score_ocr_health(md)
    assert "table-collapse" not in h["flags"]
    assert h["score"] == 100


# ── degenerate numeric sequence ─────────────────────────────────────────────
# The failure this detector exists for: MinerU/Datalab loses the page and
# counts instead of reading it. Nothing else in this module sees it — every
# item differs from the last, so the repetition checks (which test equality)
# pass, the text stays well-formed English, and the doc used to score 100.

SEQ_MD = (
    "Schedule-F: All that piece and parcel of the immovable property being "
    "land and building measuring an extent of 1830 Sq.ft or thereabouts, "
    "comprised in Punja S.No.131/1A part, Block No.25, Patta No.1454, "
    "Sengulam Village, Thirumangalam Taluk, Madurai District, "
    "West by: " + ", ".join(str(n) for n in range(26, 101)) + "."
)


def test_counting_loop_flags_degenerate_sequence():
    h = score_ocr_health(SEQ_MD)
    assert "degenerate-sequence" in h["flags"]
    d = h["details"]["degenerate_sequence"]
    assert d["items"] == 75 and d["step_run"] == 75
    assert d["sample"].startswith("26, 27, 28")
    assert h["score"] == 100 - 35


def test_counting_loop_is_invisible_to_the_repetition_checks():
    # Guards the reason this flag had to exist: no other detector fires.
    h = score_ocr_health(SEQ_MD)
    assert h["flags"] == ["degenerate-sequence"]


def test_short_number_list_does_not_flag():
    # Boundaries really are written like this. A handful of plot numbers is
    # prose, not a loop.
    md = "Bounded on the West by Plot Nos. 26, 27, 28, 29 and 30 of the layout."
    assert score_ocr_health(md)["flags"] == []


def test_indian_grouped_amounts_do_not_flag():
    md = ("Upset Price: Schedule D: Rs.2,34,00,000/- and Schedule F: "
          "Rs.58,00,000/-. Total dues Rs.18,76,61,564.00 with EMD "
          "Rs.23,40,000/- and Rs.5,80,000/- respectively.")
    assert score_ocr_health(md)["flags"] == []


def test_survey_number_list_does_not_flag():
    # Long, but the items are survey numbers, not a +1 count — and well under
    # the unordered-run bar.
    md = ("Comprised in S.Nos. 131/1A, 131/1B, 132/2, 133/4, 140/7, 141/9, "
          "142/3 and 145/6 of Sengulam Village.")
    assert score_ocr_health(md)["flags"] == []


def test_serial_numbers_across_table_cells_do_not_flag():
    # A grid numbering its lots 1…40 is layout, not a loop: runs never span
    # cells, so each <td> is judged alone.
    rows = "".join(f"<tr><td>{i}</td><td>Lot {i} borrower and survey number"
                   f"</td></tr>" for i in range(1, 41))
    assert "degenerate-sequence" not in score_ocr_health(f"<table>{rows}</table>")["flags"]


def test_serial_numbers_across_markdown_pipes_do_not_flag():
    md = "| " + " | ".join(str(i) for i in range(1, 41)) + " |"
    assert "degenerate-sequence" not in score_ocr_health(md)["flags"]


def test_long_unordered_number_dump_flags():
    # Not ascending, so the counting rule misses it — but 40 bare numbers in
    # one sentence is not a boundary description either.
    md = "West by: " + ", ".join(str((i * 37) % 900) for i in range(40)) + "."
    h = score_ocr_health(md)
    assert "degenerate-sequence" in h["flags"]
    assert h["details"]["degenerate_sequence"]["items"] == 40


def test_degenerate_sequence_stacks_with_other_flags():
    h = score_ocr_health(SEQ_MD + " <|content_end|>")
    assert set(h["flags"]) == {"degenerate-sequence", "token-leak"}
    assert h["score"] == 100 - 35 - 40


# ── per-block health (score_block_health) ───────────────────────────────────

def test_block_health_flags_inline_repetition():
    # The real "Guduvanchery, Guduvanchery, …" loop, isolated to one block.
    h = score_block_health("Guduvanchery, " * 30)
    assert "repetition" in h["flags"]
    assert h["score"] < 100


def test_block_health_clean_text_scores_100():
    h = score_block_health("Place: Chennai\nDate: 07.07.2026")
    assert h["flags"] == []
    assert h["score"] == 100


def test_block_health_flags_a_counting_loop():
    # Per-block scoring points the reviewer at the exact block to re-extract.
    h = score_block_health(SEQ_MD)
    assert h["flags"] == ["degenerate-sequence"]
    assert h["score"] == 100 - 35


def test_block_health_token_leak_and_foreign_script():
    assert "token-leak" in score_block_health("text <|content_end|> more")["flags"]
    assert "foreign-script" in score_block_health("land at 中国银行 survey no 5")["flags"]


def test_block_health_ignores_document_level_flags():
    # table-collapse / truncated are whole-document verdicts — one block being a
    # big (even unclosed) table is normal and must not flag at block level.
    big = "<table>" + "".join(
        f"<tr><td>lot {i} borrower survey extent village taluk district</td></tr>"
        for i in range(60)) + "</table>"
    assert score_block_health(big)["flags"] == []
    assert score_block_health("<table><tr><td>row</td></tr>")["flags"] == []


def test_block_health_empty_is_unscored():
    assert score_block_health("")["score"] is None
    assert score_block_health(None)["score"] is None
    assert score_block_health("   ")["flags"] == []


# ── score_freshly_loaded: the live path folds the ink verdict in ─────────────
# The regression: every live caller (loader, re-ingest, re-extract, block
# edits) scored text only, so a notice with half its ink unread was written
# back as health 100 / no flags while the annotator's Ink tab — measuring the
# same blocks live — said missing-region. Neo4j and R2 are stubbed; the page
# is drawn with Pillow so the test states exactly where the ink is.

import io
import json

import pytest

import pipeline.ocr_health as OH
from pipeline.ink_coverage import MISSING_REGION_MIN_RATIO

W = H = 400
WORD_W, WORD_H, GAP_X, GAP_Y = 16, 7, 6, 5
CLEAN_MD = "Notice is hereby given that the property will be sold on as is basis."


def _page(ink_boxes):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    for x0, y0, x1, y1 in ink_boxes:
        y = y0 * H
        while y + WORD_H <= y1 * H:
            x = x0 * W
            while x + WORD_W <= x1 * W:
                d.rectangle([x, y, x + WORD_W, y + WORD_H], fill="black")
                x += WORD_W + GAP_X
            y += WORD_H + GAP_Y
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _two_column_page():
    return _page([(0.05, 0.1, 0.45, 0.9), (0.55, 0.1, 0.95, 0.9)])


def _block(x0, y0, x1, y1, text="x"):
    return {"page": 1, "bbox": [x0, y0, x1, y1], "label": "Text", "text": text}


@pytest.fixture
def live(monkeypatch):
    """Stub Neo4j + R2. Returns (doc, written, fetched) — mutate ``doc`` to
    shape the Document row, read ``written`` for what reached Neo4j."""
    doc = {"file_path": "n/notice.png", "filename": "notice.png",
           "markdown": CLEAN_MD, "public_url": "https://r2/notice.png",
           "blocks_json": json.dumps({"blocks": [_block(0.02, 0.05, 0.48, 0.95)]}),
           "blocks_source": None}
    written: list[dict] = []
    fetched: list[str] = []
    source = {"bytes": _two_column_page()}

    def _read(cypher, params=None, **kw):
        assert "d.blocks AS blocks_json" in cypher     # the live path asks for the source
        return [doc] if doc["file_path"] in (params or {}).get("file_paths", []) else []

    def _write(cypher, params=None):
        assert "ink_uncovered_ratio" in cypher
        written.extend(params["rows"])
        return []

    def _fetch(url):
        fetched.append(url)
        return source["bytes"]

    monkeypatch.setattr(OH, "run_read_query", _read)
    monkeypatch.setattr(OH, "run_query", _write)
    monkeypatch.setattr(OH, "_fetch_source", _fetch)
    return doc, written, fetched, source


def test_live_path_flags_a_dropped_column(live):
    doc, written, fetched, _ = live
    assert OH.score_freshly_loaded([doc["file_path"]]) == 1
    row = written[0]
    assert fetched == [doc["public_url"]]
    assert "missing-region" in row["flags"]
    assert row["score"] == 100 - OH.PENALTY["missing-region"]
    assert row["ink_scored"] is True
    assert row["ratio"] == pytest.approx(0.5, abs=0.05)


def test_live_path_clears_the_flag_once_the_column_is_read(live):
    """A reviewer adding the missing box re-scores through the same call, so
    the flag has to go away as well as come."""
    doc, written, _, _ = live
    doc["blocks_json"] = json.dumps([_block(0.02, 0.05, 0.98, 0.95)])   # bare list too
    OH.score_freshly_loaded([doc["file_path"]])
    row = written[0]
    assert row["flags"] == []
    assert row["score"] == 100
    assert row["ink_scored"] is True
    assert row["ratio"] < MISSING_REGION_MIN_RATIO


def test_unfetchable_source_still_writes_the_text_score(live):
    doc, written, _, source = live
    source["bytes"] = None
    OH.score_freshly_loaded([doc["file_path"]])
    row = written[0]
    assert row["score"] == 100 and row["flags"] == []
    assert "ink_scored" not in row and "ratio" not in row     # ink fields untouched


def test_unreadable_source_is_not_a_missing_region(live):
    doc, written, _, source = live
    source["bytes"] = b"not an image"
    OH.score_freshly_loaded([doc["file_path"]])
    row = written[0]
    assert row["flags"] == [] and "ink_scored" not in row


def test_backfilled_blocks_are_not_measured_against_another_engines_text(live):
    doc, written, fetched, _ = live
    doc["blocks_source"] = "datalab-backfill"
    OH.score_freshly_loaded([doc["file_path"]])
    assert fetched == []
    assert written[0]["flags"] == [] and "ink_scored" not in written[0]


def test_no_blocks_or_no_url_means_no_fetch(live):
    doc, written, fetched, _ = live
    doc["blocks_json"] = None
    OH.score_freshly_loaded([doc["file_path"]])
    doc["blocks_json"] = json.dumps([_block(0, 0, 1, 1)])
    doc["public_url"] = None
    OH.score_freshly_loaded([doc["file_path"]])
    assert fetched == []
    assert all(r["flags"] == [] and "ink_scored" not in r for r in written)


def test_text_flags_and_the_ink_flag_stack(live):
    doc, written, _, _ = live
    doc["markdown"] = CLEAN_MD + " <|content_end|>"
    OH.score_freshly_loaded([doc["file_path"]])
    row = written[0]
    assert set(row["flags"]) == {"token-leak", "missing-region"}
    assert row["score"] == 100 - OH.PENALTY["token-leak"] - OH.PENALTY["missing-region"]
