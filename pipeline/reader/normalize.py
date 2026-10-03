"""Numbers, dates, areas and possession from verbatim quotes — by code.

The model quotes; this module parses. A quote whose digits do not hold
together is ILLEGIBLE, never a guessed number: "35.15,000" (a reserve price
read through a broken OCR cell), "Rs.5O,000" (a letter O inside a figure).
That is the field-level OCR uncertainty signal — it puts the field in the
review queue with the garbled quote as evidence instead of writing a wrong
rupee amount into the graph.

Every function returns ``Norm(value, state)`` where state is ``ok`` (value
set), ``none`` (nothing to parse) or ``illegible`` (there is a figure but it
cannot be trusted).
"""
from __future__ import annotations

import re
from typing import NamedTuple

from pipeline.measures import parse_area as _parse_area
from pipeline.measures import parse_quantity


class Norm(NamedTuple):
    value: object
    state: str          # ok | none | illegible


NONE = Norm(None, "none")
ILLEGIBLE = Norm(None, "illegible")

_MULT = {"rupees": 1, "lakh": 100_000, "crore": 10_000_000}
_UNIT_WORD = re.compile(r"\b(lakh|lakhs|lac|lacs|crore|crores|cr)\b\.?", re.I)
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
# A figure broken by the OCR: a decimal point followed later by a comma
# ("35.15,000"), or a letter that only looks like a digit inside a digit run.
_BROKEN_DECIMAL = re.compile(r"\d+\.\d+,\d")
_LETTER_IN_DIGITS = re.compile(r"\d[OoIl|]\d|\d[OoIl|],|,[OoIl|]\d")
_DOUBLE_DECIMAL = re.compile(r"\d+\.\d+\.\d")


#: Indian digit grouping (1,00,00,000) with a full stop read in a comma
#: slot: "35.15,000" is 35,15,000 and "3.51,500" is 3,51,500 (gold 750348).
#: The repair is narrow — a dot followed by exactly two digits and then a
#: comma with three — so a real decimal ("Rs.7.00 Lakhs", "12.50") never
#: matches. Callers learn of it through :func:`grouping_repaired` and mark
#: the value, so the receipt says the figure was read through an OCR fault.
_GROUPING_DOT = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{2}),(\d{3})(?![\d.])")


def grouping_repaired(quote: str | None) -> bool:
    return bool(quote and _GROUPING_DOT.search(str(quote)))


def money(quote: str | None, unit: str | None = None) -> Norm:
    """Integer rupees from a quote and a unit (from the figure or its column
    header). A unit word inside the quote wins over ``unit``."""
    if not quote or not str(quote).strip():
        return NONE
    s = _GROUPING_DOT.sub(r"\1,\2,\3", str(quote))
    if _LETTER_IN_DIGITS.search(s) or _BROKEN_DECIMAL.search(s) or _DOUBLE_DECIMAL.search(s):
        return ILLEGIBLE
    m = _NUM.search(s)
    if not m:
        return NONE
    tok = m.group(0).replace(",", "")
    # Two decimal points ("12.50.000") is another broken cell.
    if tok.count(".") > 1:
        return ILLEGIBLE
    try:
        val = float(tok)
    except ValueError:
        return ILLEGIBLE
    # A unit word counts only right after the figure ("Rs.70.00 Lakhs"). The
    # amount in words that follows many figures ("Rs. 2889000/- (Rupees Twenty
    # Eight Lakhs ...)", gold 752245) names lakhs too, and multiplying by it
    # read a 28.89 lakh reserve as 2,88,900 crore.
    tail = s[m.end():m.end() + 14]
    near = _UNIT_WORD.search(tail)
    u = None
    if near and not re.match(r"\s*/?-?\s*\(", tail):
        w = near.group(1).lower()
        u = "crore" if w.startswith("cr") else "lakh"
    elif unit in _MULT:
        u = unit
    # A figure already written in full rupees is never scaled again: a lakh
    # column holds figures like 28.89, a crore column figures like 1.25.
    if (u == "lakh" and val >= 100_000) or (u == "crore" and val >= 10_000_000):
        u = "rupees"
    val *= _MULT.get(u or "rupees", 1)
    if val <= 0:
        return ILLEGIBLE
    return Norm(int(round(val)), "ok")


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_DMY = re.compile(r"(?<!\d)(\d{1,2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{4}|\d{2})(?!\d)")
_D_MON_Y = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s*[-./ ]?\s*([A-Za-z]{3,9})\.?,?\s*[-./ ]?\s*(\d{4})")
_MON_D_Y = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})")
_TIME = re.compile(r"(?<!\d)(\d{1,2})[.:](\d{2})\s*(?:hrs|hours|h)?\s*(am|pm|a\.m\.|p\.m\.)?", re.I)
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?$")


def _month(word: str) -> int | None:
    return _MONTHS.get(word[:3].lower())


def _time_of(s: str, after: int) -> str:
    m = _TIME.search(s, after)
    if not m:
        return ""
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower().replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return ""
    return f"T{h:02d}:{mi:02d}"


def date(quote: str | None, iso_hint: str | None = None) -> Norm:
    """ISO date (with THH:MM when the quote states a time) from a quote.

    ``iso_hint`` is the model's own reading; it is used only when every one
    of its digit groups occurs in the quote, so a hallucinated date cannot
    ride in on a real sentence. Day-month order is dd/mm (Indian notices)."""
    if not quote or not str(quote).strip():
        return NONE
    s = str(quote)
    if iso_hint:
        m = _ISO.match(iso_hint.strip())
        if m:
            y, mo, d = m.group(1), m.group(2), m.group(3)
            digits = re.findall(r"\d+", s)
            words = s.lower()
            month_ok = (mo in digits or mo.lstrip("0") in digits
                        or any(_month(w) == int(mo) for w in re.findall(r"[A-Za-z]{3,9}", words)))
            day_ok = d in digits or d.lstrip("0") in digits
            year_ok = y in digits or y[2:] in digits
            if month_ok and day_ok and year_ok:
                t = f"T{m.group(4)}:{m.group(5)}" if m.group(4) else _time_of(s, 0)
                return Norm(f"{y}-{mo}-{d}{t}", "ok")
    m = _DMY.search(s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        y = int(y) if len(y) == 4 else 2000 + int(y)
        if mo > 12 and d <= 12:      # mm/dd slipped in
            d, mo = mo, d
        if 1 <= d <= 31 and 1 <= mo <= 12:
            return Norm(f"{y:04d}-{mo:02d}-{d:02d}{_time_of(s, m.end())}", "ok")
        return ILLEGIBLE
    m = _D_MON_Y.search(s)
    if m and _month(m.group(2)):
        d, mo, y = int(m.group(1)), _month(m.group(2)), int(m.group(3))
        if 1 <= d <= 31:
            return Norm(f"{y:04d}-{mo:02d}-{d:02d}{_time_of(s, m.end())}", "ok")
    m = _MON_D_Y.search(s)
    if m and _month(m.group(1)):
        d, mo, y = int(m.group(2)), _month(m.group(1)), int(m.group(3))
        if 1 <= d <= 31:
            return Norm(f"{y:04d}-{mo:02d}-{d:02d}{_time_of(s, m.end())}", "ok")
    return ILLEGIBLE if re.search(r"\d", s) else NONE


class Area(NamedTuple):
    value: float | None
    unit: str | None
    sqft: float | None


def area(quote: str | None) -> Norm:
    """(value, canonical unit, sqft) via pipeline.measures.parse_area."""
    if not quote or not str(quote).strip():
        return NONE
    v, u, sq = _parse_area(str(quote))
    if v is None:
        return ILLEGIBLE if re.search(r"\d", str(quote)) else NONE
    return Norm(Area(v, u, sq), "ok")


def possession(quote: str | None) -> Norm:
    """symbolic | physical | constructive when the quote commits to ONE; a
    menu of types or the Canara conditional block yields none."""
    if not quote or not str(quote).strip():
        return NONE
    from pipeline.gap_fill import stated_possession_kinds
    kinds = stated_possession_kinds(str(quote))
    if len(kinds) == 1:
        return Norm(next(iter(kinds)), "ok")
    return NONE


def digits_consistent(quote: str) -> bool:
    """False when a figure in the quote is visibly OCR-broken."""
    s = str(quote or "")
    return not (_LETTER_IN_DIGITS.search(s) or _BROKEN_DECIMAL.search(s)
                or _DOUBLE_DECIMAL.search(s))


__all__ = ["Norm", "NONE", "ILLEGIBLE", "money", "date", "area", "possession",
           "digits_consistent", "parse_quantity", "Area"]
