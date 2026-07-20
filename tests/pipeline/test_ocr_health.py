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
