"""
api/review/grounding.py
-----------------------
Re-anchor LangExtract grounding when the markdown moves underneath it.

``pipeline/load_extractions.py`` stores each entity's char span (``start`` /
``end``) straight from langextract's ``char_interval``: offsets into the exact
``d.markdown`` string the extraction ran against. Every rewrite of that string
— a re-ingest, a block edit, a per-block re-extract, adopting a Datalab parse
over a MinerU one — shifts every character after the edit, and the stored
offsets go on pointing at the old positions.

Nothing errors when that happens, which is the problem. ``_build_fields``
reports ``grounded`` from "does a start exist", the extraction UI maps each
field to whatever text now sits at those offsets
(``web/review_extraction.html``), and a reviewer is shown a value anchored to a
passage that does not contain it. A wrong anchor is worse than a missing one:
it looks like evidence.

**A stored span is not a quotation.** A sweep of 15,884 live spans found only
73% where ``markdown[start:end]`` equals the entity text; 83% are within
similarity 85. langextract normalizes as it extracts — collapsing a newline
inside a phone number, inserting a space after a comma, turning a dash into a
comma — and sometimes *composes* a value that was never contiguous on the page
("Reserve Price Rs.75,33,000/-, EMD Rs.7,53,300/-, Bid Increment Rs.75,000/-"
assembled from three corners of a table). So its span is an approximate
alignment by nature, and treating "the slice doesn't equal the text" as "this
anchor is broken" would condemn a third of a healthy corpus.

That gives the rule this module follows: **only a changed markdown makes a
stored span worthless.** The caller says whether the text has been rewritten
since the extraction ran — the same staleness signal
``api/review/extraction.extraction_stale`` already computes from
``markdown_reextracted_at`` / ``markdown_loaded_at`` — and that decides what
happens when a span cannot be confirmed:

  * markdown unchanged → keep langextract's own span (``ANCHOR_UNVERIFIED``).
    It is approximate, but it is the only reading of the page anyone has, and a
    fuzzy guess of ours is not an improvement on it.
  * markdown changed → the span refers to a string that no longer exists, so
    drop it (``ANCHOR_LOST``) rather than leave it pointing somewhere untrue.

Per entity, cheapest first:

  1. **verify** — is ``markdown[start:end]`` still the same passage, allowing
     for the normalization above (similarity ≥ ``VERIFY_MIN_SCORE``)? On
     unchanged markdown this confirms nearly every span with one slice.
  2. **exact** — find the text verbatim. The search starts at the previous
     entity's end and wraps, so the repeated values a notice is full of
     ("Rs.5,00,000" once per lot) re-anchor in document order instead of all
     collapsing onto the first occurrence. Safe on unchanged markdown too: an
     exact hit beats an approximate alignment.
  3. **fuzzy** — only worth risking once the stored span is known worthless.
     Aligns on similarity (the same rapidfuzz alignment
     ``api/review/markdown_match.py`` uses for property highlights) above
     ``FUZZY_MIN_SCORE``, guarded by ``FUZZY_MIN_CHARS`` — a 4-character value
     fuzzy-matches half a page.
  4. **keep or drop**, per the rule above.

The value, attributes and any reviewer correction are never touched. Only the
claim about *where* the value came from is revised.

Pure and DB-free, the way ``api/review/markdown_match.py`` is.
"""
from __future__ import annotations

from collections import Counter

from rapidfuzz import fuzz

from api.review.markdown_match import _snap_to_word_boundaries

#: The stored span still lands on this text (exactly, or within the
#: normalization langextract applies) — nothing moved.
ANCHOR_STORED = "stored"
#: Found the text verbatim elsewhere; the stored offsets were stale or off.
ANCHOR_RELOCATED = "relocated"
#: Matched on similarity, not equality — the OCR itself changed the characters.
ANCHOR_FUZZY = "fuzzy"
#: Could not be confirmed, but the markdown has not changed, so langextract's
#: own approximate span is kept. Usually a composed value that was never
#: contiguous on the page.
ANCHOR_UNVERIFIED = "unverified"
#: The markdown was rewritten and the text cannot be found in it. The span is
#: dropped rather than guessed.
ANCHOR_LOST = "lost"
#: The extraction never grounded this entity (langextract returned no span).
ANCHOR_NONE = "none"

#: How close the stored slice must be to the entity text to count as the same
#: passage. Set from the corpus sweep in the module docstring: exact matches
#: are 73%, and the shoulder between "normalized the same passage" and
#: "different passage entirely" sits well below this.
VERIFY_MIN_SCORE = 85.0
#: Similarity floor for step 3, on rapidfuzz's 0-100 scale. Deliberately far
#: above the 75 ``markdown_match`` uses: that probe is a whole property
#: description, where a middling score still means "this paragraph"; here it is
#: a single short value, where a middling score means nothing at all.
FUZZY_MIN_SCORE = 90.0
#: ...and below this length, similarity carries no signal — "No.5" is inside a
#: dozen unrelated strings on any notice.
FUZZY_MIN_CHARS = 8


def _slice(markdown: str, start, end) -> str | None:
    """The stored span's text, or None when the span isn't usable at all."""
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    if start < 0 or end > len(markdown) or end <= start:
        return None
    return markdown[start:end]


def _verify(markdown: str, text: str, start, end) -> bool:
    """True when the stored span still points at this entity's passage.

    Similarity, not equality — see the module docstring on why an exact test
    would condemn a healthy corpus.
    """
    got = _slice(markdown, start, end)
    if got is None:
        return False
    return got == text or fuzz.ratio(got, text) >= VERIFY_MIN_SCORE


def _exact(markdown: str, text: str, cursor: int) -> int | None:
    """Offset of ``text``, preferring the first occurrence at or after
    ``cursor`` and falling back to a search from the top.

    The cursor is what keeps a notice's repeated values in order: entities
    arrive in document order, so lot 3's reserve price re-anchors after lot
    2's rather than onto lot 1's identical figure.
    """
    i = markdown.find(text, cursor)
    if i != -1:
        return i
    i = markdown.find(text)
    return i if i != -1 else None


def _fuzzy(markdown: str, text: str) -> tuple[int, int] | None:
    """Best similarity alignment of ``text`` in ``markdown``, or None.

    Case-insensitive, because a re-OCR routinely changes only case, and the
    span comes back against the original string so the caller's offsets stay
    usable.

    Snapped outward with ``markdown_match``'s own snapper: the same
    partial-ratio trimming that leaves property highlights ragged ("All"
    clipped off the front) clips a value's first character here, and one
    definition of "grow to the token edge, never across an HTML tag" is better
    than two that can drift apart.
    """
    if len(text) < FUZZY_MIN_CHARS:
        return None
    al = fuzz.partial_ratio_alignment(text.lower(), markdown.lower())
    if al is None or al.score < FUZZY_MIN_SCORE:
        return None
    start, end = al.dest_start, al.dest_end
    if end <= start or start < 0 or end > len(markdown):
        return None
    return _snap_to_word_boundaries(markdown, start, end)


def anchor_entity(markdown: str, text: str, start, end, cursor: int = 0, *,
                  markdown_changed: bool = False):
    """Re-anchor one entity. Returns ``(start, end, status)``.

    ``markdown_changed`` says whether the text was rewritten since the
    extraction ran; it decides only what happens when the span cannot be
    confirmed (keep it, or drop it) — see the module docstring.
    """
    if not text:
        return None, None, ANCHOR_NONE
    if _verify(markdown, text, start, end):
        return start, end, ANCHOR_STORED
    hit = _exact(markdown, text, cursor)
    if hit is not None:
        return hit, hit + len(text), ANCHOR_RELOCATED
    if not markdown_changed:
        # Unconfirmed, but the page under it has not moved: langextract's own
        # alignment is the best reading of it that exists. A fuzzy guess here
        # would be replacing an approximate anchor with a speculative one.
        if _slice(markdown, start, end) is None:
            return None, None, ANCHOR_NONE
        return start, end, ANCHOR_UNVERIFIED
    span = _fuzzy(markdown, text)
    if span is not None:
        return span[0], span[1], ANCHOR_FUZZY
    return None, None, ANCHOR_LOST


def reanchor(ents: list[dict], markdown: str | None, *,
             markdown_changed: bool = False) -> tuple[list[dict], dict]:
    """Re-anchor a document's entities against its current markdown.

    Returns ``(entities, summary)``. Each entity is a shallow copy carrying
    corrected ``start`` / ``end`` and an ``anchor`` status; the input list and
    its dicts are never mutated, so a caller that only wants the reading (a
    queue count, an audit) cannot accidentally persist one.

    ``summary`` counts the statuses, plus ``checked`` (entities considered) and
    ``moved`` (those whose offsets actually changed) — enough for a caller to
    say "3 of 41 fields moved, 1 lost" without walking the list again.

    **No markdown, no judgement.** With nothing to verify against, the stored
    spans come back exactly as they are. Absence of evidence must not drop
    grounding — the same rule ``pipeline/ocr_health.py`` follows for an
    unmeasured ``missing-region``.
    """
    counts: Counter = Counter()
    out: list[dict] = []
    cursor = 0
    moved = 0
    for e in ents:
        if not isinstance(e, dict):
            continue
        text = e.get("text") or ""
        start, end = e.get("start"), e.get("end")
        if not markdown:
            status = ANCHOR_STORED if isinstance(start, int) else ANCHOR_NONE
            new_start, new_end = start, end
        else:
            new_start, new_end, status = anchor_entity(
                markdown, text, start, end, cursor,
                markdown_changed=markdown_changed)
            if isinstance(new_end, int):
                cursor = new_end
        if (new_start, new_end) != (start, end):
            moved += 1
        counts[status] += 1
        out.append({**e, "start": new_start, "end": new_end, "anchor": status})
    summary = dict(counts)
    summary["checked"] = len(out)
    summary["moved"] = moved
    return out, summary
