"""Locate a model's quote in the text — inside the lot it was assigned to.

The old reader stored whatever the model said and hoped the offsets matched;
73% did. Here nothing is stored that code cannot find: a quote is searched
for in ITS OWN segment only (or, for a table field, its own cell), first
exactly, then with case and whitespace folded, then by bounded fuzzy
alignment for quotes long enough to carry signal. What is not found is
``None``: the caller drops it and logs it. Because the search window is the
segment, a value from the neighbouring lot cannot pass — that is the
cross-lot contamination guarantee.

Fuzzy matching is deliberately strict (score ≥ 92, quote ≥ 12 characters):
"No.5" is inside a dozen unrelated strings on any notice.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from functools import cached_property

from rapidfuzz import fuzz

from api.review.grounding import _fold_text, fold
from api.review.markdown_match import _snap_to_word_boundaries

FUZZY_MIN_SCORE = 92.0
FUZZY_MIN_CHARS = 12


@dataclass(frozen=True)
class Located:
    start: int
    end: int
    anchor: str          # exact | fold | fuzzy


@dataclass
class Window:
    """A slice of the notice a quote may be found in. Folds lazily, once."""
    md: str
    lo: int = 0
    hi: int | None = None

    def __post_init__(self) -> None:
        if self.hi is None or self.hi > len(self.md):
            self.hi = len(self.md)
        self.lo = max(0, min(self.lo, self.hi))

    @property
    def text(self) -> str:
        return self.md[self.lo:self.hi]

    @cached_property
    def folded(self) -> tuple[str, list[int]]:
        return fold(self.text)

    def sub(self, lo: int, hi: int) -> "Window":
        return Window(self.md, max(self.lo, lo), min(self.hi, hi))


def locate(window: Window, quote: str | None, cursor: int = 0) -> Located | None:
    """Span of ``quote`` inside ``window`` (absolute offsets into the notice),
    preferring the first occurrence at or after ``cursor`` (absolute), or
    None. Invariant: the returned span lies within [window.lo, window.hi) and
    ``md[start:end]`` equals the quote exactly, or equals it after folding,
    or ``anchor == "fuzzy"``."""
    if not quote or not str(quote).strip():
        return None
    q = str(quote).strip()
    text = window.text
    if not text:
        return None
    rel = max(0, cursor - window.lo)

    i = text.find(q, rel)
    if i == -1:
        i = text.find(q)
    if i != -1:
        return Located(window.lo + i, window.lo + i + len(q), "exact")

    body, index = window.folded
    tf = _fold_text(q)
    if tf and body:
        j = body.find(tf, bisect_left(index, rel) if rel else 0)
        if j == -1:
            j = body.find(tf)
        if j != -1:
            return Located(window.lo + index[j],
                           window.lo + index[j + len(tf) - 1] + 1, "fold")

    # Fuzzy runs over the FOLDED text (a whitespace run in a table cell costs
    # nothing) and maps back through the fold index. Two guards: length, so
    # "No.5" cannot match anything; and every digit of the quote must survive
    # in the matched span in order, so "Rs.5,00,000" can never settle on the
    # neighbouring lot's "Rs.75,00,000" — that is the misbinding this module
    # exists to prevent, and similarity alone scores it 92.
    if len(q) >= FUZZY_MIN_CHARS and tf and body:
        al = fuzz.partial_ratio_alignment(tf, body)
        if al is not None and al.score >= FUZZY_MIN_SCORE:
            fs, fe = al.dest_start, al.dest_end
            if 0 <= fs < fe <= len(body):
                s, e = index[fs], index[fe - 1] + 1
                s, e = _snap_to_word_boundaries(text, s, e)
                if _digits(text[s:e]) == _digits(q):
                    return Located(window.lo + s, window.lo + e, "fuzzy")
    return None


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def contains(window: Window, value: str | None) -> bool:
    """Whether ``value`` occurs in the window after folding — the test a place
    part (district, state, ...) must pass to be kept: stated, not inferred."""
    if not value or not str(value).strip():
        return False
    body, _ = window.folded
    return _fold_text(str(value)) in body


__all__ = ["Located", "Window", "locate", "contains", "FUZZY_MIN_SCORE",
           "FUZZY_MIN_CHARS"]
