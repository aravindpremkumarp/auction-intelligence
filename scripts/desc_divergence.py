#!/usr/bin/env python3
"""Flag notice descriptions that look like they belong to a SIBLING listing.

The original divergence check asked one question per listing: does the notice
description we wrote (``AuctionProperty.description``, ``description_source =
'notice'``) still share words with the portal text the scraper found
(``website_description``)? Low overlap meant "suspicious".

That test is blind exactly where the risk lives. Sibling lots on one sale
notice are usually flats in one building or plots in one layout: they quote the
same survey number, the same locality, the same boundaries, often the same
borrower. A listing matched to its NEIGHBOUR's lot still scores high absolute
overlap, so the check passes on precisely the error it exists to catch.

So the question here is comparative, not absolute:

    does this notice description match THIS listing's portal text better than
    it matches any SIBLING listing's portal text?

If a sibling wins, the lot<->listing match that produced the description
(pipeline/apply_extractions.py::match_lots_to_listings) probably put this
description on the wrong listing.

Two ideas make that work:

1. **Discount the shared ground.** Tokens most siblings share — the common
   survey number, the village, the bank, the boilerplate — cannot separate
   anybody, so they are removed from both sides first. What survives is the
   per-unit identifier: a door number, a flat number, a property tax
   assessment number. Same trick ``apply_extractions._id_tokens`` uses to
   break ties between sibling flats, applied here as a check rather than a
   matcher.

2. **Normalise per portal text, not per notice text.** Each score divides by
   the size of the PORTAL side, so a sibling with a longer blurb cannot win
   just by having more words to hit.

A notice with one listing has no sibling to compare against; those fall back
to the original absolute-overlap buckets and are reported separately.

READ-ONLY. Writes a JSONL report and prints a summary; nothing is written to
Neo4j — a flagged row is a question for the lot-match review queue, not a
verdict.

    NEO4J_HTTP_API=1 python -m scripts.desc_divergence
    NEO4J_HTTP_API=1 python -m scripts.desc_divergence --limit 200
    NEO4J_HTTP_API=1 python -m scripts.desc_divergence --margin 0.15
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from pipeline.text_overlap import description_overlap as absolute_overlap
from pipeline.text_overlap import tokenize

OUT = Path("output/desc_divergence.jsonl")

#: A sibling must beat the listing's own score by at least this much before the
#: row is flagged. Token overlap is noisy at the margins — one stray OCR token
#: is worth several points on a short portal blurb — and a flagged row costs a
#: human's attention, so a bare lead is not evidence.
DEFAULT_MARGIN = 0.10

#: Absolute-overlap buckets, kept from the original check for the notices that
#: have no sibling to compare against. (lower_bound, label).
ABSOLUTE_BUCKETS = (
    (0.50, "similar"),
    (0.25, "moderate"),
    (0.10, "different"),
    (0.00, "very_different"),
)

# ── pure: tokens ─────────────────────────────────────────────────────────────

# `tokenize` and `absolute_overlap` live in pipeline/text_overlap.py rather
# than here: the same two functions now gate the write in
# pipeline/apply_extractions.description_verdict, and a report that scored
# tokens differently from the guard it informs would be worse than no report.


def shared_tokens(sets: list[set[str]]) -> set[str]:
    """Tokens more than half the siblings carry, i.e. the shared ground.

    Strictly "more than half", never "at least half": with two siblings the
    latter would call a token in either one shared and erase every
    distinguishing token there is. With two siblings this is exactly the
    intersection; with twelve it also sheds boilerplate that happens to be
    missing from one bad OCR pass.
    """
    if len(sets) < 2:
        return set()
    df: Counter[str] = Counter()
    for s in sets:
        df.update(s)
    n = len(sets)
    return {t for t, c in df.items() if c * 2 > n}


def match_score(notice: set[str], portal: set[str]) -> float:
    """Fraction of a listing's distinguishing portal tokens that the notice
    description actually contains.

    Dividing by the PORTAL side is what makes scores comparable across
    siblings: the notice side is held fixed while the portal side varies, so a
    sibling with a wordier blurb gains no advantage. An empty portal side
    scores 0.0 — nothing to claim, so it can never win a comparison.
    """
    if not portal:
        return 0.0
    return round(len(notice & portal) / len(portal), 4)


def bucket(overlap: float) -> str:
    for lower, label in ABSOLUTE_BUCKETS:
        if overlap >= lower:
            return label
    return "very_different"


# ── pure: per-notice assessment ──────────────────────────────────────────────

def assess_notice(listings: list[dict], margin: float = DEFAULT_MARGIN) -> list[dict]:
    """Score every listing on one notice against every sibling's portal text.

    ``listings``: ``[{"aid": str, "notice": str, "portal": str}, ...]`` — all
    the listings sharing one Document, where ``notice`` is the description we
    wrote from the sale notice and ``portal`` is the scraper's own text.

    Returns one row per listing with a ``verdict``:
      ``ok``               the listing's own portal text wins
      ``misassigned``      a sibling wins by at least ``margin``
      ``close``            a sibling leads, but inside the margin
      ``indistinguishable`` no distinguishing tokens survived on either side
      ``no_siblings``      one listing on the notice — absolute check only
      ``no_text``          the listing is missing a notice or portal description
    """
    rows: list[dict] = []
    usable: list[dict] = []
    for x in listings:
        if (x.get("notice") or "").strip() and (x.get("portal") or "").strip():
            usable.append(x)
        else:
            rows.append({"auction_id": x["aid"], "verdict": "no_text",
                         "reason": "missing notice or portal description"})

    if not usable:
        return rows

    notice_tok = [tokenize(x["notice"]) for x in usable]
    portal_tok = [tokenize(x["portal"]) for x in usable]

    # Single listing on the notice: nothing to compare against, so fall back to
    # the original absolute question.
    if len(usable) == 1:
        ov = absolute_overlap(usable[0]["notice"], usable[0]["portal"])
        rows.append({"auction_id": usable[0]["aid"], "verdict": "no_siblings",
                     "absolute_overlap": ov, "bucket": bucket(ov),
                     "reason": "only listing on this notice"})
        return rows

    shared_notice = shared_tokens(notice_tok)
    shared_portal = shared_tokens(portal_tok)
    distinct_notice = [s - shared_notice for s in notice_tok]
    distinct_portal = [s - shared_portal for s in portal_tok]

    for i, x in enumerate(usable):
        scores = [match_score(distinct_notice[i], p) for p in distinct_portal]
        own = scores[i]
        rivals = [(s, j) for j, s in enumerate(scores) if j != i]
        best, best_j = max(rivals) if rivals else (0.0, None)

        row = {
            "auction_id": x["aid"],
            "own_score": own,
            "best_sibling_score": best,
            "best_sibling_id": usable[best_j]["aid"] if best_j is not None else None,
            "lead": round(best - own, 4),
            "absolute_overlap": absolute_overlap(x["notice"], x["portal"]),
            "siblings": len(usable),
        }
        row["bucket"] = bucket(row["absolute_overlap"])

        if not distinct_portal[i] and all(not p for p in distinct_portal):
            row["verdict"] = "indistinguishable"
            row["reason"] = "no distinguishing portal tokens on any sibling"
        elif best - own >= margin:
            row["verdict"] = "misassigned"
            row["reason"] = (f"sibling {row['best_sibling_id']} matches this "
                             f"notice text better ({best:.2f} vs {own:.2f})")
        elif best > own:
            row["verdict"] = "close"
            row["reason"] = (f"sibling leads by {best - own:.2f}, "
                             f"under the {margin:.2f} margin")
        else:
            row["verdict"] = "ok"
            row["reason"] = "own portal text matches best"
        rows.append(row)

    return rows


# ── Neo4j I/O ────────────────────────────────────────────────────────────────

def fetch_work(limit: int | None = None) -> list[dict]:
    """Every notice-sourced listing, grouped by the Document it came from.

    Gated on ``description_source = 'notice'`` because a listing still showing
    the portal's own text has no notice description to be wrong about. A
    listing can link to more than one scan of the same notice; grouping by
    Document keeps each scan's listings together, which is the grouping the
    lot match itself used.

    The driver import is deferred to here on purpose: everything above this
    line is pure token arithmetic, and its tests should not need a Neo4j
    driver (or a populated .env) on the path to run.
    """
    from api.neo4j_client import run_read_query

    return run_read_query(
        """
        MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
        WHERE a.description_source = 'notice'
        RETURN d.filename AS filename,
               collect({aid:    a.auction_id,
                        notice: a.description,
                        portal: a.website_description,
                        lot:    [(a)-[:IS_LOT]->(_l:Lot) | _l.lot_key][0]})
                       AS listings
        ORDER BY d.filename
        """ + (f" LIMIT {int(limit)}" if limit else ""),
        max_rows=20_000, timeout=120.0)


# ── main ─────────────────────────────────────────────────────────────────────

def run(limit: int | None = None, margin: float = DEFAULT_MARGIN) -> int:
    work = fetch_work(limit)
    print(f"Notices with notice-sourced descriptions: {len(work)}")

    rows: list[dict] = []
    for doc in work:
        by_aid = {x["aid"]: x for x in doc["listings"]}
        for r in assess_notice(doc["listings"], margin=margin):
            r["filename"] = doc["filename"]
            r["resolved_lot_key"] = (by_aid.get(r["auction_id"]) or {}).get("lot")
            rows.append(r)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    verdicts = Counter(r["verdict"] for r in rows)
    print(f"\nListings scored: {len(rows)}")
    print(f"{'verdict':<20} {'listings':>9}")
    for v, c in verdicts.most_common():
        print(f"{v:<20} {c:>9}")

    sib = [r for r in rows if r["verdict"] in ("ok", "close", "misassigned")]
    if sib:
        flagged = verdicts["misassigned"]
        pct = 100.0 * flagged / len(sib)
        print(f"\nOn multi-listing notices: {flagged}/{len(sib)} "
              f"({pct:.1f}%) look misassigned")

    solo = [r for r in rows if r["verdict"] == "no_siblings"]
    if solo:
        print(f"\nSingle-listing notices ({len(solo)}), absolute overlap:")
        for _lo, label in ABSOLUTE_BUCKETS:
            n = sum(1 for r in solo if r["bucket"] == label)
            print(f"  {label:<16} {n:>6}")

    print(f"\nwrote {OUT}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=None,
                    help="only the first N notices (by filename)")
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN,
                    help=f"lead a sibling needs to flag a row (default {DEFAULT_MARGIN})")
    args = ap.parse_args()
    return run(limit=args.limit, margin=args.margin)


if __name__ == "__main__":
    sys.exit(main())
