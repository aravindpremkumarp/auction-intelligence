#!/usr/bin/env python3
"""Dry-run the proposed description-completeness score against the live graph.

Computes the score from docs/superpowers/specs/2026-06-05-description-
completeness-scoring-design.md for every AuctionProperty WITHOUT writing
anything back, then reports the score distribution and, for a sweep of
auto-verify thresholds, how many properties would auto-clear.

Nothing is mutated — this is read-only. Run it before implementing the
scoring change so the thresholds can be tuned on real data.

    python -m scripts.dryrun_description_completeness
    python -m scripts.dryrun_description_completeness --limit 500 --csv out.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sys

from rapidfuzz import fuzz

from api.neo4j_client import read_session
from api.review.markdown_match import _normalize_for_match, description_coverage, strip_field_bleed

SENTENCE_SPLIT = re.compile(r"(?<=[.;:])\s+|\n+")
MIN_SENTENCE_CHARS = 25
SENTENCE_HIT = 85.0  # partial_ratio at/above which a reference sentence counts as covered


def word_similarity(website: str | None, extracted: str | None) -> float:
    """Fraction of the eauctionsindia.com reference's sentences whose wording is
    recovered in the notice extraction."""
    w = strip_field_bleed(website or "")
    e_norm = _normalize_for_match(extracted or "")
    if not e_norm:
        return 0.0
    sentences = [s for s in SENTENCE_SPLIT.split(w) if len(s.strip()) >= MIN_SENTENCE_CHARS]
    if not sentences:
        return 1.0  # nothing meaningful to compare against
    covered = sum(
        1 for s in sentences if fuzz.partial_ratio(_normalize_for_match(s), e_norm) >= SENTENCE_HIT
    )
    return covered / len(sentences)


def length_adequacy(website: str | None, extracted: str | None) -> float:
    """1.0 once the notice extraction is at least ~0.9x the website length;
    scales down when the notice is suspiciously shorter (likely partial)."""
    w_len = len(strip_field_bleed(website or ""))
    e_len = len(extracted or "")
    if w_len == 0:
        return 1.0
    return min((e_len / w_len) / 0.9, 1.0)


def end_reached(prop: dict) -> float:
    """Schedule-tail guard from boundary (or door-number) presence."""
    n = sum(
        1
        for k in ("boundary_north", "boundary_south", "boundary_east", "boundary_west")
        if prop.get(k)
    )
    if n == 4:
        return 1.0
    if n in (2, 3):
        return 0.6
    if prop.get("door_numbers_old") or prop.get("door_numbers_new"):
        return 0.6
    return 0.0


def score(prop: dict) -> dict:
    website = prop.get("website_description") or prop.get("description_scraped")
    # E is ONLY extracted_description (the notice extraction). a.description is
    # NOT a safe fallback: it is seeded from the website text and only becomes
    # the notice description after apply_descriptions runs, so using it would
    # compare website-vs-website and inflate recall to ~1.0.
    extracted = prop.get("extracted_description")
    markdown = prop.get("markdown")

    words = word_similarity(website, extracted)
    length = length_adequacy(website, extracted)
    end = end_reached(prop)
    completeness = round(0.50 * words + 0.20 * length + 0.30 * end, 2)
    anchor, _span = description_coverage(website, markdown)

    return {
        "auction_id": prop.get("auction_id"),
        "notice_type": prop.get("notice_type"),
        "source": prop.get("description_source"),
        "verified": bool(prop.get("verified")),
        "has_extracted": bool((extracted or "").strip()),
        "old_completeness": prop.get("old_completeness"),
        "word_similarity": round(words, 2),
        "length_adequacy": round(length, 2),
        "end_reached": end,
        "completeness": completeness,
        "anchor_score": anchor,
    }


def auto_verifiable(row: dict, comp_min: float, anchor_min: float) -> bool:
    return (
        row["has_extracted"]
        and row["source"] != "human"
        and row["notice_type"] == "single"
        and row["completeness"] >= comp_min
        and row["anchor_score"] >= anchor_min
    )


FETCH = """
MATCH (a:AuctionProperty)
OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(d:Document)
WITH a, head(collect(d)) AS d
RETURN a.auction_id               AS auction_id,
       a.website_description      AS website_description,
       a.description_scraped      AS description_scraped,
       a.extracted_description    AS extracted_description,
       a.description              AS description,
       a.description_source       AS description_source,
       a.description_completeness AS old_completeness,
       coalesce(a.description_verified, false) AS verified,
       a.boundary_north AS boundary_north, a.boundary_south AS boundary_south,
       a.boundary_east  AS boundary_east,  a.boundary_west  AS boundary_west,
       a.door_numbers_old AS door_numbers_old, a.door_numbers_new AS door_numbers_new,
       d.notice_type AS notice_type,
       d.markdown    AS markdown
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="cap properties scanned (0 = all)")
    ap.add_argument("--csv", help="write per-property scores to this CSV")
    args = ap.parse_args()

    cypher = FETCH + (f"\nLIMIT {int(args.limit)}" if args.limit else "")
    with read_session() as s:
        rows = [score(dict(r)) for r in s.run(cypher)]

    if not rows:
        print("No properties found.")
        return 1

    total = len(rows)
    buckets = {"0.0-0.5": 0, "0.5-0.7": 0, "0.7-0.85": 0, "0.85-1.0": 0}
    for r in rows:
        c = r["completeness"]
        key = "0.0-0.5" if c < 0.5 else "0.5-0.7" if c < 0.7 else "0.7-0.85" if c < 0.85 else "0.85-1.0"
        buckets[key] += 1

    print(f"\nScanned {total} properties.\n")
    print("completeness distribution:")
    for k, v in buckets.items():
        print(f"  {k:>9}: {v:5d}  ({100 * v / total:4.1f}%)")

    print("\nauto-clear rate by threshold (single, source!=human, has extraction):")
    print(f"  {'comp_min':>8} {'anchor_min':>10} {'auto':>7} {'%queue':>8}")
    for comp_min in (0.80, 0.85, 0.90):
        for anchor_min in (75, 80, 85):
            auto = sum(1 for r in rows if auto_verifiable(r, comp_min, anchor_min))
            print(f"  {comp_min:>8.2f} {anchor_min:>10d} {auto:>7d} {100 * auto / total:>7.1f}%")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote per-property scores to {args.csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
