"""How much wording two descriptions of a property share.

One definition, two callers: ``pipeline/apply_extractions.py`` gates the
description write on it, and ``scripts/desc_divergence.py`` reports on it. A
report that scored tokens differently from the guard it informs would be worse
than no report, so neither owns the function.

Deliberately free of Neo4j, config and every other import — the guard runs
inside a pipeline stage and the report runs from the CLI, but the arithmetic
itself is pure and its tests should need nothing on the path.
"""
from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str | None) -> set[str]:
    """Lowercased alphanumeric tokens, single characters dropped.

    Numbers are kept: a door number, a plot number and a survey number are
    exactly what distinguishes one property from another, and dropping them
    would leave only the boilerplate every notice shares.

    No stopword list. Tokens that carry no signal are the ones every sibling
    lot shares, and the callers that care remove those on the evidence of the
    notice itself rather than a list someone has to maintain.
    """
    if not text:
        return set()
    return {t for t in _WORD.findall(text.lower()) if len(t) > 1}


def description_overlap(a: str | None, b: str | None) -> float:
    """Jaccard overlap between two descriptions: shared tokens over all tokens.

    Symmetric on purpose. A one-sided containment score would read a notice
    schedule that merely quotes the right locality as a good match; requiring
    both sides to be mostly the same words is what makes a near-zero score
    mean "these describe different property" rather than "one is longer".

    Returns 0.0 when either side is empty. That is not a low score, it is the
    absence of one — a caller gating on this must check for the empty side
    itself rather than read silence as disagreement.
    """
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return round(len(ta & tb) / len(ta | tb), 4)
