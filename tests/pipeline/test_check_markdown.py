"""Label-free OCR-failure checks for sale-notice markdown.

Pure-function, DB-free. See pipeline/check_markdown.py.
"""
from __future__ import annotations

import glob
import os

from pipeline.check_markdown import check_markdown, main

# A clean, English-only notice in the shape of evals/fixtures/*.txt.
CLEAN = (
    "SALE NOTICE. E-Auction under SARFAESI Act 2002. Authorised Officer of "
    "Canara Bank, Settihalli Branch will sell the property on 07.05.2026 for "
    "recovery of Rs. 44,67,205/-. Reserve Price Rs 1,34,00,000/-, EMD Rs "
    "13,40,000/-. Site no 258 HIG I, MIG II. Boundaries: East: Road."
)


def _codes(text: str) -> set[str]:
    return {i["code"] for i in check_markdown(text)["issues"]}


def test_clean_notice_scores_100_no_issues():
    report = check_markdown(CLEAN)
    assert report["score"] == 100
    assert report["issues"] == []


def test_empty_input_is_clean():
    report = check_markdown("")
    assert report["score"] == 100
    assert report["issues"] == []
    assert check_markdown(None)["score"] == 100


def test_foreign_currency_flagged_high():
    report = check_markdown("Reserve Price €1,34,00,000/- and EMD £13,400.")
    issue = next(i for i in report["issues"] if i["code"] == "foreign_currency")
    assert issue["severity"] == "high"
    assert issue["count"] == 2          # € and £
    assert report["score"] < 100


def test_rupee_symbols_are_not_flagged():
    # Both the modern ₹ and the legacy ₨ are accepted.
    assert _codes("Reserve Price ₹1,34,00,000/- EMD ₨13,400") == set()


def test_dollar_sign_is_foreign_currency():
    assert "foreign_currency" in _codes("recovery of $44,000")


def test_chinese_characters_flagged_as_foreign_script():
    report = check_markdown("Authorised Officer 通知 of Canara Bank")
    issue = next(i for i in report["issues"] if i["code"] == "foreign_script")
    assert issue["severity"] == "high"
    assert issue["count"] == 2
    assert "CJK" in issue["msg"]


def test_other_foreign_scripts_flagged():
    for snippet in ("Bank Привет here", "Bank مرحبا here", "Bank Γειά here"):
        assert "foreign_script" in _codes(snippet), snippet


def test_indic_script_is_low_not_high():
    # Kannada place name: flagged low (may be legit), never high.
    report = check_markdown("village ಸತ್ಯಮಂಗಲ Kasaba Hobli")
    codes = {i["code"]: i["severity"] for i in report["issues"]}
    assert codes.get("indic_script") == "low"
    assert "foreign_script" not in codes


def test_repeated_word_flagged():
    report = check_markdown("Contact Manager, Canara Bank Bank, Settihalli.")
    issue = next(i for i in report["issues"] if i["code"] == "repeated_word")
    assert issue["severity"] == "med"
    assert '"bank"' in issue["msg"]


def test_repeated_word_is_case_insensitive_and_handles_dash():
    assert "repeated_word" in _codes("the THE property")
    assert "repeated_word" in _codes("Bank - Bank branch")


def test_repeated_word_not_flagged_across_sentence_or_in_phrase():
    # "is ... is" is not consecutive; sentence boundary should not trip it.
    assert "repeated_word" not in _codes('"As is where is", "As is What is"')
    assert "repeated_word" not in _codes("the bank. Bank of India")


def test_repeated_char_run_flagged_but_double_letters_are_fine():
    assert "repeated_char_run" in _codes("the Saaale notice")     # 3+ → flag
    assert "repeated_char_run" not in _codes("address committee")  # 2 → fine


def test_roman_numerals_not_flagged_as_repeated_chars():
    # Site numbers like "HIG III" / "MIG III" are legitimate.
    assert "repeated_char_run" not in _codes("Site no 258 HIG III, MIG III")


def test_www_in_url_not_flagged_but_real_typo_is():
    assert "repeated_char_run" not in _codes("bid at https://www.bankeauctions.com")
    assert "repeated_char_run" in _codes("https://wwww.typo.com")


def test_replacement_char_flagged_high():
    report = check_markdown("recovery of Rs. 44,67,2�5/-")
    issue = next(i for i in report["issues"] if i["code"] == "replacement_char")
    assert issue["severity"] == "high"


def test_locations_report_line_and_col():
    report = check_markdown("line one ok\nbad €100 here")
    loc = next(i for i in report["issues"]
               if i["code"] == "foreign_currency")["locations"][0]
    assert loc["line"] == 2
    assert "€" in loc["snippet"]


def test_score_floors_at_zero_with_many_issues():
    # Fire every code (high×3 + med×2 + low×2 = 103 penalty) → floored, not negative.
    report = check_markdown("通知 €100 £ ¥ Bank Bank Saaale � Привет ಕ\x07")
    assert {i["code"] for i in report["issues"]} == set(report["stats"]["by_code"])
    assert report["score"] == 0


def test_real_fixtures_are_clean():
    """Guard against false positives on the real scraped notices we ship —
    these are clean English transcriptions and should score a perfect 100."""
    here = os.path.dirname(__file__)
    fixtures = glob.glob(os.path.join(here, "..", "..", "evals", "fixtures", "*.txt"))
    assert fixtures, "expected evals/fixtures/*.txt"
    for path in fixtures:
        with open(path, encoding="utf-8") as fh:
            report = check_markdown(fh.read())
        assert report["score"] == 100, \
            f"{os.path.basename(path)} unexpectedly flagged: {report['issues']}"


def test_cli_exit_code_and_stdin(monkeypatch, capsys):
    # Clean stdin → exit 0; dirty stdin (foreign currency = high) → exit 1.
    monkeypatch.setattr("sys.stdin", _Stdin(CLEAN))
    assert main(["-"]) == 0
    monkeypatch.setattr("sys.stdin", _Stdin("Reserve €100"))
    assert main(["-"]) == 1
    out = capsys.readouterr().out
    assert "foreign_currency" in out


class _Stdin:
    def __init__(self, text: str):
        self._text = text

    def read(self) -> str:
        return self._text
