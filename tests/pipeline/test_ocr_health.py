"""Intrinsic OCR-health scoring (pipeline/ocr_health.py).

Pure-function, DB-free. Fixtures mirror the real MinerU failure modes
observed on full-page ruled notices (repetition loops, control-token
leakage, truncated tables) and the legitimate look-alikes that must NOT
flag (per-lot boilerplate repeated *non*-consecutively).
"""
from __future__ import annotations

from pipeline.ocr_health import score_ocr_health


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
