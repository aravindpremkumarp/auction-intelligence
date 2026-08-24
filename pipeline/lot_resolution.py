"""
pipeline/lot_resolution.py
---------------------------
Decide which lot inside a multi-lot sale notice a portal listing actually is.

A sale notice's `Document` can bundle several `Lot`s — 655 notices in the live
corpus do, touching 1,988 `AuctionProperty` listings (~two-thirds of the
portal). `api/agent3/common.py::scope_of()` treats every value on such a
listing as "notice-scoped" rather than "lot-scoped": true about the notice,
not confirmed about this specific property. Most of the time it IS
confirmable — every `Lot`'s `Auction` node and every `AuctionProperty` already
carry their own `reserve_price_num`, an exact numeric join nobody queries.

**Reserve price is the primary signal, not borrower name.** Verified on
auction 796269 (Sriperumbudur, 6 lots): the listing's reserve price matches
exactly one lot's, and only that one. Reserve price is clean, already-parsed
numeric data on both sides; borrower name is OCR'd free text with real
variance ("M/s. Sri Vaaru Traders" vs "M/s. Sri Vaaru Traders, Represented by
its Proprietor Mr. Maneesh A P"). So borrower is the tiebreaker, used only
when reserve price alone cannot decide — never the primary key.

Same two-tier shape as :mod:`pipeline.entity_resolution`: a rule that decides
on its own, and everything the rule can't decide left alone rather than
guessed at.
"""
from __future__ import annotations

from typing import Any

#: A listing resolves on reserve price alone when its reserve exactly matches
#: exactly one candidate lot. Reserve prices are recorded to the rupee, so
#: exact equality (post-rounding, to dodge float noise from parsing) is a
#: real signal, not a coincidence — two independently-priced lots on the same
#: notice landing on the same rupee figure is not something the corpus shows.
RESULT_RESERVE = "reserve"
#: Reserve price alone did not decide (zero or 2+ candidates matched); a
#: unique borrower-name match among the surviving candidates broke the tie.
RESULT_RESERVE_BORROWER = "reserve+borrower"
#: No reserve match at all, but a unique borrower-name match among every lot
#: on the notice — reserve price was missing or wrong on one side.
RESULT_BORROWER = "borrower"

#: Borrower-name similarity floor for a tiebreak match. Deliberately higher
#: than `entity_resolution.REVIEW_MIN_SCORE` (88, and advisory even then):
#: this score decides automatically, so it demands the score that separates
#: "the same party" from merely two names that share common words.
BORROWER_MATCH_MIN_SCORE = 90.0


def lot_match_key(auction_id: str, lot_key: str) -> str:
    """Stable key for one (listing, lot) match — directed, not order-
    independent like a merge pair, because a listing resolves to a specific
    lot, not the other way round."""
    return f"lot-match:{auction_id}|{lot_key}"


def _round_reserve(value: Any) -> int | None:
    """Reserve prices arrive as floats from Neo4j; round to the rupee so a
    parse artefact (41479000.0 vs 41479000.0000001) never breaks an equality
    check that should hold."""
    if value is None:
        return None
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def _borrower_matches(name: str, candidates: list[dict]) -> list[dict]:
    """Candidates whose borrower text plausibly names the same party as
    ``name``. ``token_set_ratio`` rather than `token_sort_ratio`: a lot's
    party list is a long legal string ("M/s. X, Represented by its Proprietor
    Mr. Y") that CONTAINS the listing's shorter borrower name ("M/s. X")
    rather than closely resembling it word-for-word — token_set_ratio scores
    that containment highly where token_sort_ratio would not.
    """
    if not name:
        return []
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return []

    from pipeline.entity_resolution import normalize

    target = normalize(name)
    if not target:
        return []
    out = []
    for c in candidates:
        for raw in c.get("borrowers") or ():
            if not raw:
                continue
            score = fuzz.token_set_ratio(target, normalize(raw))
            if score >= BORROWER_MATCH_MIN_SCORE:
                out.append(c)
                break
    return out


def resolve_lot(*, listing_reserve: Any, listing_borrower: str | None,
                candidates: list[dict]) -> dict:
    """Decide which lot (if any) one listing on a multi-lot notice is.

    ``candidates`` is every `Lot` on the listing's `Document`:
    ``[{"lot_key": str, "reserve": float | None,
       "borrowers": list[str]}, ...]``.

    Returns ``{"lot_key": str | None, "method": str | None, "reason": str}``.
    ``lot_key`` is ``None`` when the notice genuinely can't be disambiguated
    with today's signals — the caller leaves `scope_of()` at "notice" and
    writes no decision, exactly today's behavior.
    """
    if not candidates:
        return {"lot_key": None, "method": None,
                "reason": "no candidate lots on this document"}

    target = _round_reserve(listing_reserve)
    reserve_matches = (
        [c for c in candidates if _round_reserve(c.get("reserve")) == target]
        if target is not None else [])

    if len(reserve_matches) == 1:
        return {"lot_key": reserve_matches[0]["lot_key"],
                "method": RESULT_RESERVE,
                "reason": "reserve price matches exactly one lot"}

    pool = reserve_matches if reserve_matches else candidates
    tied = len(reserve_matches) >= 2
    borrower_matches = _borrower_matches(listing_borrower or "", pool)

    if len(borrower_matches) == 1:
        method = RESULT_RESERVE_BORROWER if tied else RESULT_BORROWER
        reason = (f"{len(reserve_matches)} lots tied on reserve price; "
                   f"borrower name broke the tie" if tied else
                   "no unique reserve match; borrower name matched exactly one lot")
        return {"lot_key": borrower_matches[0]["lot_key"], "method": method,
                "reason": reason}

    if tied:
        reason = (f"{len(reserve_matches)} lots share this reserve price and "
                   f"borrower name did not narrow it to one")
    elif reserve_matches == [] and target is not None:
        reason = "no lot's reserve price matches this listing's"
    else:
        reason = "listing has no reserve price to match on"
    return {"lot_key": None, "method": None, "reason": reason}
