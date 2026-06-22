"""
pipeline/check_markdown.py
--------------------------
Label-free quality checker for OCR'd sale-notice markdown.

This flags the *intrinsic* transcription failures we keep seeing in MinerU /
OCR output for SARFAESI auction notices — the ones you can spot without any
ground truth, just by looking at the text:

    foreign_currency    a non-INR currency symbol (€ £ ¥ …) where ₹ / Rs
                        belongs — usually corrupts the price fields. A bare '$'
                        is NOT flagged: MinerU emits LaTeX '$…$' math spans.
    foreign_script      characters from a script that never legitimately
                        appears in an English/Indic notice (Chinese/Han,
                        Japanese, Korean, Cyrillic, Arabic, Greek, …)
    repeated_word       a word printed twice in a row ("Canara Bank Bank")
    repeated_char_run   the same letter run 3+ times inside a word ("Saaale")
    replacement_char    the Unicode replacement glyph '�' (a hard decode fail)
    indic_script        Indic-script characters (Kannada/Tamil/Devanagari/…) —
                        *may* be a legitimate local-language name, flagged
                        low so a reviewer can glance, not auto-rejected
    control_char        stray non-printable control characters

It complements the two checks already in the pipeline, neither of which
catches the above:
  * pipeline/score_markdown.py  — scores markdown by how well it reproduces the
                                  scraped website description (needs ground truth)
  * pipeline/validators.py      — validates the *structured extraction output*,
                                  not the raw markdown text

Output mirrors validators.py: a 0–100 score (100 = clean) plus a list of issue
dicts. The score is display/triage only — it tells a reviewer which markdowns
to eyeball first; nothing is auto-rejected.

Usage
~~~~~
    python -m pipeline.check_markdown notice.md            # one or more files
    python -m pipeline.check_markdown notices/*.md
    cat notice.md | python -m pipeline.check_markdown -    # stdin
    python -m pipeline.check_markdown --json notice.md     # machine-readable
    python -m pipeline.check_markdown --quiet notices/*.md # only show problems

Exit code is non-zero when any file has a high-severity issue (so this can gate
a load step), or when any file scores below --fail-under N if that's given.

    from pipeline.check_markdown import check_markdown
    report = check_markdown(markdown)
    # -> {"score": int, "issues": [...], "stats": {...}}
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
import re
import sys
import unicodedata

# Penalty (0-100) per severity; score = 100 - sum(penalties over fired codes),
# floored at 0. Same scale/weights as pipeline/validators.py.
_PENALTY = {"high": 25, "med": 10, "low": 4}

# Currency symbols we treat as fine in this corpus:
#   ₹ / ₨   the rupee (modern INDIAN RUPEE SIGN + legacy RUPEE SIGN)
#   $       MinerU wraps math in LaTeX '$…$' / '$$…$$' spans — percentages
#           ($10 \%$), superscripts (20$^{th}$), degrees (35$^{\circ}$) — so a
#           bare '$' is markup noise here, not a foreign-currency symbol.
_OK_CURRENCY = {"₹", "₨", "$"}

# Unicode replacement character — an unambiguous decode/OCR failure.
_REPLACEMENT = "�"

# Roman-numeral letters (matched case-insensitively), so neither a site number
# like "MIG III" nor a lowercase list marker like "iii) PAN card" is mistaken
# for a stuttered letter run.
_ROMAN = set("IVXLCDM")

# Repeated-letter runs that are legitimate, not OCR stutter. "www" appears in
# the e-auction portal URLs these notices carry; a real typo like "wwww" still
# trips because only the exact 3-letter run is whitelisted.
_OK_RUNS = {"www"}

# Codepoint ranges, (lo, hi, kind, name). "foreign" = should never appear in
# one of these notices; "indic" = a local-language script that might be a real
# name but is worth a glance. Latin (incl. extensions) and combining marks all
# live below 0x370 and are treated as fine (see _script_of fast path).
_SCRIPT_RANGES: tuple[tuple[int, int, str, str], ...] = (
    (0x0370, 0x03FF, "foreign", "Greek"),
    (0x0400, 0x052F, "foreign", "Cyrillic"),
    (0x0530, 0x058F, "foreign", "Armenian"),
    (0x0590, 0x05FF, "foreign", "Hebrew"),
    (0x0600, 0x06FF, "foreign", "Arabic"),
    (0x0750, 0x077F, "foreign", "Arabic"),
    (0x0900, 0x097F, "indic", "Devanagari"),
    (0x0980, 0x09FF, "indic", "Bengali"),
    (0x0A00, 0x0A7F, "indic", "Gurmukhi"),
    (0x0A80, 0x0AFF, "indic", "Gujarati"),
    (0x0B00, 0x0B7F, "indic", "Oriya"),
    (0x0B80, 0x0BFF, "indic", "Tamil"),
    (0x0C00, 0x0C7F, "indic", "Telugu"),
    (0x0C80, 0x0CFF, "indic", "Kannada"),
    (0x0D00, 0x0D7F, "indic", "Malayalam"),
    (0x0D80, 0x0DFF, "indic", "Sinhala"),
    (0x0E00, 0x0E7F, "foreign", "Thai"),
    (0x10A0, 0x10FF, "foreign", "Georgian"),
    (0x1100, 0x11FF, "foreign", "Hangul"),
    (0x3040, 0x309F, "foreign", "Hiragana"),
    (0x30A0, 0x30FF, "foreign", "Katakana"),
    (0x3130, 0x318F, "foreign", "Hangul"),
    (0x3000, 0x303F, "foreign", "CJK"),
    (0x3400, 0x4DBF, "foreign", "CJK"),
    (0x4E00, 0x9FFF, "foreign", "CJK"),
    (0xAC00, 0xD7AF, "foreign", "Hangul"),
    (0xF900, 0xFAFF, "foreign", "CJK"),
    (0xFF00, 0xFFEF, "foreign", "Fullwidth"),
    (0x20000, 0x2A6DF, "foreign", "CJK"),
)

# A word: 2+ Unicode letters (no digits/underscore) — keeps "Rs", "Bank" but
# skips numbers and lone letters.
_WORD = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
# 3+ of the same letter in a row.
_CHAR_RUN = re.compile(r"([^\W\d_])\1{2,}", re.UNICODE)

_SEVERITY = {
    "foreign_currency": "high",
    "foreign_script": "high",
    "replacement_char": "high",
    "repeated_word": "med",
    "repeated_char_run": "med",
    "indic_script": "low",
    "control_char": "low",
}
_SEVERITY_ORDER = {"high": 0, "med": 1, "low": 2}

# Cap stored example locations per code so a wall of foreign characters can't
# blow up the report (the count still reflects every occurrence).
_MAX_LOCATIONS = 25


def _script_of(cp: int) -> tuple[str, str] | None:
    """Return (kind, script_name) for a codepoint, or None if it's Latin /
    punctuation / otherwise fine. Fast-path everything below Greek."""
    if cp < 0x0370:
        return None
    for lo, hi, kind, name in _SCRIPT_RANGES:
        if lo <= cp <= hi:
            return kind, name
    return None


def _locate(text: str, line_starts: list[int], start: int, end: int) -> dict:
    """Map a [start, end) char span to a 1-based line/col plus a trimmed,
    single-line snippet of context for human-readable output."""
    idx = bisect.bisect_right(line_starts, start) - 1
    line_no = idx + 1
    line_start = line_starts[idx]
    nl = text.find("\n", start)
    line_end = len(text) if nl == -1 else nl
    col = start - line_start + 1

    win = 36
    s = max(line_start, start - win)
    e = min(line_end, end + win)
    body = re.sub(r"\s+", " ", text[s:e]).strip()
    pre = "…" if s > line_start else ""
    suf = "…" if e < line_end else ""
    return {
        "line": line_no,
        "col": col,
        "match": text[start:end],
        "snippet": f"{pre}{body}{suf}",
    }


def check_markdown(text: str | None) -> dict:
    """Scan OCR'd notice markdown for intrinsic transcription failures.

    Returns {"score": int, "issues": [...], "stats": {...}}, mirroring
    pipeline/validators.py. Each issue is one dict per fired code:
        {code, severity, msg, count, locations: [{line, col, match, snippet}]}
    """
    text = text or ""
    line_starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(i + 1)

    # code -> {"count": int, "locations": [..], "detail": Counter-ish}
    found: dict[str, dict] = {}

    def record(code: str, start: int, end: int, detail: str | None = None) -> None:
        bucket = found.setdefault(code, {"count": 0, "locations": [], "detail": {}})
        bucket["count"] += 1
        if detail:
            bucket["detail"][detail] = bucket["detail"].get(detail, 0) + 1
        if len(bucket["locations"]) < _MAX_LOCATIONS:
            bucket["locations"].append(_locate(text, line_starts, start, end))

    # ── per-character checks: currency, scripts, replacement, control ─────────
    for i, ch in enumerate(text):
        if ch in _OK_CURRENCY or ch in "\n\r\t":
            continue
        if ch == _REPLACEMENT:
            record("replacement_char", i, i + 1)
            continue
        cat = unicodedata.category(ch)
        if cat == "Sc":                       # any currency symbol that isn't ₹/₨
            record("foreign_currency", i, i + 1, detail=ch)
            continue
        if cat == "Cc":                       # control char (tab/newline already skipped)
            record("control_char", i, i + 1, detail=f"U+{ord(ch):04X}")
            continue
        script = _script_of(ord(ch))
        if script is None:
            continue
        kind, name = script
        record("foreign_script" if kind == "foreign" else "indic_script",
               i, i + 1, detail=name)

    # ── repeated word: same word twice in a row, separated by a single space or
    #    a spaced hyphen/slash. Requiring a single space (not a newline or a
    #    2-space block join) drops false hits at heading→body boundaries like
    #    "…IMMOVABLE PROPERTY  Property No:1" while keeping real inline stutters
    #    like "Rupees Rupees" / "Erode Erode".
    prev: re.Match | None = None
    for m in _WORD.finditer(text):
        if prev is not None and m.group().lower() == prev.group().lower():
            sep = text[prev.end():m.start()]
            if sep == " " or re.fullmatch(r" ?[-–/] ?", sep):
                record("repeated_word", prev.start(), m.end(), detail=m.group().lower())
        prev = m

    # ── repeated char run: 3+ same letter, excluding Roman numerals (MIG III) ─
    for m in _CHAR_RUN.finditer(text):
        if m.group(1).upper() in _ROMAN or m.group().lower() in _OK_RUNS:
            continue
        record("repeated_char_run", m.start(), m.end(), detail=m.group())

    # ── assemble issues + score ──────────────────────────────────────────────
    issues: list[dict] = []
    for code, bucket in found.items():
        severity = _SEVERITY[code]
        issues.append({
            "code": code,
            "severity": severity,
            "msg": _message(code, bucket),
            "count": bucket["count"],
            "locations": bucket["locations"],
        })
    issues.sort(key=lambda x: (_SEVERITY_ORDER[x["severity"]], x["code"]))

    score = max(0, 100 - sum(_PENALTY[i["severity"]] for i in issues))
    return {
        "score": score,
        "issues": issues,
        "stats": {
            "chars": len(text),
            "lines": len(line_starts),
            "n_issues": len(issues),
            "by_code": {i["code"]: i["count"] for i in issues},
        },
    }


def _message(code: str, bucket: dict) -> str:
    n = bucket["count"]
    detail = bucket["detail"]
    plural = "s" if n != 1 else ""
    if code == "foreign_currency":
        syms = " ".join(sorted(detail))
        return f"{n} non-INR currency symbol{plural} ({syms}) — expected ₹ / Rs"
    if code == "foreign_script":
        names = ", ".join(f"{k}×{v}" for k, v in sorted(detail.items()))
        return f"{n} character{plural} from a foreign script ({names})"
    if code == "indic_script":
        names = ", ".join(f"{k}×{v}" for k, v in sorted(detail.items()))
        return f"{n} Indic-script character{plural} ({names}) — verify vs. noise"
    if code == "replacement_char":
        return f"{n} Unicode replacement char{plural} '�' (decode failure)"
    if code == "control_char":
        names = ", ".join(f"{k}×{v}" for k, v in sorted(detail.items()))
        return f"{n} stray control character{plural} ({names})"
    if code == "repeated_word":
        words = ", ".join(f'"{k}"' for k in sorted(detail)[:5])
        return f"{n} consecutive duplicate word{plural} ({words})"
    if code == "repeated_char_run":
        runs = ", ".join(f'"{k}"' for k in sorted(detail)[:5])
        return f"{n} run{plural} of a repeated letter ({runs})"
    return f"{n} occurrence{plural}"


# ── CLI ──────────────────────────────────────────────────────────────────────

def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _print_report(path: str, report: dict, *, show_clean: bool) -> None:
    issues = report["issues"]
    if not issues and not show_clean:
        return
    head = f"{path}  score={report['score']}  issues={len(issues)}"
    print(head)
    for issue in issues:
        print(f"  [{issue['severity']:>4}] {issue['code']}: {issue['msg']}")
        for loc in issue["locations"]:
            print(f"         L{loc['line']}:{loc['col']}  {loc['snippet']}")
        extra = issue["count"] - len(issue["locations"])
        if extra > 0:
            print(f"         … and {extra} more")


def _expand(paths: list[str]) -> list[str]:
    """Expand any literal globs the shell didn't (Windows / quoted patterns)."""
    out: list[str] = []
    for p in paths:
        if p == "-":
            out.append(p)
            continue
        hits = glob.glob(p)
        out.extend(sorted(hits) if hits else [p])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Flag common OCR/transcription failures in sale-notice markdown.")
    ap.add_argument("paths", nargs="+", help="markdown file(s), or - for stdin")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    ap.add_argument("--quiet", action="store_true", help="skip files with no issues")
    ap.add_argument("--fail-under", type=int, default=None, metavar="N",
                    help="exit non-zero if any file scores below N "
                         "(default gate: any high-severity issue)")
    args = ap.parse_args(argv)

    results: list[dict] = []
    bad = False
    for path in _expand(args.paths):
        try:
            text = _read(path)
        except OSError as exc:
            print(f"{path}: cannot read — {exc}", file=sys.stderr)
            bad = True
            continue
        report = check_markdown(text)
        results.append({"path": path, **report})

        if args.fail_under is not None:
            if report["score"] < args.fail_under:
                bad = True
        elif any(i["severity"] == "high" for i in report["issues"]):
            bad = True

        if not args.json:
            _print_report(path, report, show_clean=not args.quiet)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
