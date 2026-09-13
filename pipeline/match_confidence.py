"""How much a lot match is worth — the grade that sits beside `IS_LOT.method`.

`method` records WHICH signal decided that a listing is a lot;
`pipeline.apply_extractions._EXPLAIN_TEXT` renders each one as a sentence for
the review UI. Neither says how much that signal is worth, so a consumer
reading the edge as a boolean treats `description` (3 edges live) exactly like
`exact` (1,772). This module supplies the missing half.

Deliberately two fields, not one. `confidence` does not encode the REASON for
confidence — that is what `method` is for, and collapsing them would lose the
distinction between a deterministic price match and a human who opened the
notice and picked:

    method       confidence   interpretation
    exact        CONFIRMED    deterministic price match
    single       CONFIRMED    only one possible lot
    decision     CONFIRMED    a person resolved the ambiguity
    identifier   PROBABLE     a survey/door number names one lot
    borrower     INFERRED     weaker semantic signal
    description  INFERRED     weakest surviving signal

An unrecognised method grades as ``UNKNOWN``, never ``CONFIRMED``. A future
matching tier must not become high-confidence merely because somebody forgot
to update the table here, and the write path must not abort a whole batch
over a name it does not know. The loud half of that guard lives in
``tests/pipeline/test_match_confidence.py``, which fails when a reason exists
in the vocabulary without a grade — a CI failure, not a silent promotion.
"""
from __future__ import annotations

CONFIRMED = "CONFIRMED"
PROBABLE = "PROBABLE"
INFERRED = "INFERRED"
UNKNOWN = "UNKNOWN"

#: Grade per `IS_LOT.method`. Every reason that can reach an edge appears here.
MATCH_CONFIDENCE: dict[str, str] = {
    # Nothing to get wrong: one lot on the notice, or the money agrees to the
    # rupee, or a person decided.
    "single": CONFIRMED,
    "exact": CONFIRMED,
    "decision": CONFIRMED,
    # A real signal that can still land on the wrong lot. Tolerance admits a
    # rounded figure; EMD is ~10% of the reserve almost everywhere, so it
    # separates lots only where the reserve price was missing; an identifier
    # read out of the listing's own text names one lot but is only as good as
    # the text it was read from.
    "tolerance": PROBABLE,
    "emd": PROBABLE,
    "emd_tolerance": PROBABLE,
    "identifier": PROBABLE,
    # Inference. Borrower name is shared across a borrower's whole portfolio;
    # `portal_aid` is the extraction model asserting something about data it
    # cannot quote; description overlap is a similarity score; `remainder` is
    # process of elimination and nothing else.
    "borrower": INFERRED,
    "portal_aid": INFERRED,
    "description": INFERRED,
    "remainder": INFERRED,
}

#: Reasons that describe a listing left UNLINKED. They are in the explanation
#: vocabulary but can never appear on an edge, so they are graded nowhere —
#: named here so the test can tell "deliberately ungraded" from "forgotten".
UNMATCHED_REASONS: frozenset[str] = frozenset({
    "ambiguous",
    "portal_aid_conflict",
    "none",
    "no_listing_price",
    "no_lots",
})


#: Grade per `SAME_LISTING_AS.method` — the cross-portal bridge written by
#: `sources.match` (spec: docs/superpowers/specs/2026-09-12-source-adapters-design.md).
#: A separate table, not more rows in MATCH_CONFIDENCE: the names overlap
#: (`identifier`, `borrower`) but the edges differ, and so do the grades —
#: a borrower match inside a bank + reserve + day bucket is PROBABLE, while
#: the same name against a whole multi-lot notice is only INFERRED.
SAME_LISTING_CONFIDENCE: dict[str, str] = {
    # Byte-identical sale notice, or three of four boundary neighbours agree.
    "notice_bytes": CONFIRMED,
    "boundaries": CONFIRMED,
    # The same survey / door / plot / flat number, or the same party.
    "identifier": PROBABLE,
    "borrower": PROBABLE,
    # Bank + reserve price + auction day and nothing more. A same-day batch
    # sale (several lots, one borrower, one price) lands here.
    "bucket_only": INFERRED,
}


def listing_confidence_for(method: str | None) -> str:
    """Grade one `SAME_LISTING_AS.method`. Anything unrecognised is ``UNKNOWN``."""
    if not isinstance(method, str):
        return UNKNOWN
    return SAME_LISTING_CONFIDENCE.get(method, UNKNOWN)


def confidence_for(method: str | None) -> str:
    """Grade one `IS_LOT.method`. Anything unrecognised is ``UNKNOWN``.

    Total by construction: this runs inside a batch write, where raising on
    an unfamiliar name would cost every other row in the batch. The mapping
    is guarded at CI time instead.
    """
    if not isinstance(method, str):
        return UNKNOWN
    return MATCH_CONFIDENCE.get(method, UNKNOWN)
