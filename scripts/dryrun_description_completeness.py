#!/usr/bin/env python3
"""Dry-run the proposed description-completeness scoring against the live graph.

Per docs/superpowers/specs/2026-06-05-description-completeness-scoring-design.md,
completeness is decided by an LLM judge that reads the notice markdown (M) and
our extraction (E) and reports whether E is the complete property description
present in M. This script does NOT write anything back.

Two passes:
  * cheap pre-filter (free, always): contiguous-text overlap between the
    extraction E and the eauctionsindia.com description W, over every property,
    with a distribution report.
  * LLM judge (costs model calls): with --judge-sample N, runs the judge on a
    random sample of N properties so the judge quality, auto-clear rate and
    per-property cost can be checked before a full backfill.

    python -m scripts.dryrun_description_completeness
    python -m scripts.dryrun_description_completeness --judge-sample 25 --csv out.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import urllib.request

from rapidfuzz import fuzz

from api.neo4j_client import read_session
from api.review.markdown_match import _normalize_for_match, strip_field_bleed

# ── Cheap pre-filter: contiguous-text overlap (E vs eauctionsindia.com W) ──────


def text_overlap(website: str | None, extracted: str | None) -> float:
    """Contiguous-span similarity between the extraction and the website text,
    measured over continuous sentences/paragraphs rather than isolated words.

    Takes the higher of token_set_ratio (order-independent overlap) and the
    longest aligned contiguous block as a fraction of the website length, so a
    whole paragraph carried over scores high while scattered word hits do not.
    """
    w = _normalize_for_match(strip_field_bleed(website or ""))
    e = _normalize_for_match(extracted or "")
    if not w or not e:
        return 0.0
    token = fuzz.token_set_ratio(w, e) / 100.0
    al = fuzz.partial_ratio_alignment(w, e)
    contiguous = ((al.src_end - al.src_start) / len(w)) if al else 0.0
    return round(max(token, min(contiguous, 1.0)), 2)


# ── Authoritative signal: LLM completeness judge ──────────────────────────────

JUDGE_PROMPT = """You are auditing a property-description extraction from an Indian bank \
auction sales notice.

The SALES NOTICE MARKDOWN below is the source of truth — it contains the full \
legal property description (the "schedule"). We extracted one property's \
description from it. Your job: decide whether the EXTRACTED DESCRIPTION is the \
COMPLETE property description present in the notice for {target} — nothing \
missing, nothing truncated.

Rules:
- Judge only against what the notice markdown actually contains. Do NOT assume a \
fixed schedule structure.
- Do NOT penalise the extraction for containing MORE detail than a short listing \
would; extra legitimate detail is good.
- If the extraction describes a DIFFERENT property/lot than {target}, set \
wrong_property=true.
- Report exactly what is in the notice's description but missing/cut off in the \
extraction.

Return STRICT JSON only:
{{"complete": bool, "completeness": 0.0-1.0, "missing_parts": ["..."], \
"wrong_property": bool, "confidence": 0.0-1.0, "reasoning": "1-2 lines"}}

=== SALES NOTICE MARKDOWN ===
{markdown}

=== EXTRACTED DESCRIPTION ===
{extracted}
"""


def run_judge(markdown: str, extracted: str, target: str) -> dict | None:
    """Synchronous OpenRouter call for the dry-run sample. Reuses pipeline config
    + JSON parser. Returns the parsed judge verdict or None on failure."""
    from pipeline.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL
    from pipeline.ocr_extract import parse_llm_response

    prompt = JUDGE_PROMPT.format(
        target=target or "the property in this notice",
        markdown=(markdown or "")[:20000],
        extracted=extracted or "",
    )
    body = json.dumps(
        {
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1024,
            "temperature": 0.0,
        }
    ).encode()
    req = urllib.request.Request(
        f"{OPENROUTER_BASE_URL}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://bank-auction-intelligence.local",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return parse_llm_response(data["choices"][0]["message"]["content"])
    except Exception as e:  # network / parse / API — dry-run, just report
        print(f"  [judge error] {e}")
        return None


# ── Gate ──────────────────────────────────────────────────────────────────────


def auto_verifiable(row: dict, comp_min: float, conf_min: float) -> bool:
    j = row.get("judge") or {}
    return (
        row["has_extracted"]
        and row["source"] != "human"
        and row["notice_type"] == "single"
        and bool(j.get("complete"))
        and not j.get("wrong_property")
        and float(j.get("completeness") or 0) >= comp_min
        and float(j.get("confidence") or 0) >= conf_min
    )


FETCH = """
MATCH (a:AuctionProperty)
OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(d:Document)
OPTIONAL MATCH (a)-[:HAS_BORROWER]->(b:Borrower)
WITH a, head(collect(DISTINCT d)) AS d, collect(DISTINCT b.name) AS borrowers
RETURN a.auction_id               AS auction_id,
       a.title                    AS title,
       borrowers                  AS borrowers,
       a.website_description      AS website_description,
       a.description_scraped      AS description_scraped,
       a.extracted_description    AS extracted_description,
       a.description_source       AS description_source,
       a.description_completeness AS old_completeness,
       coalesce(a.description_verified, false) AS verified,
       d.notice_type AS notice_type,
       d.markdown    AS markdown
"""


def build_row(r: dict) -> dict:
    website = r.get("website_description") or r.get("description_scraped")
    # E is ONLY extracted_description (the notice extraction). a.description is
    # NOT a safe fallback: it is seeded from the website text and only becomes
    # the notice description after apply_descriptions runs.
    extracted = r.get("extracted_description")
    return {
        "auction_id": r.get("auction_id"),
        "title": r.get("title"),
        "borrowers": r.get("borrowers") or [],
        "notice_type": r.get("notice_type"),
        "source": r.get("description_source"),
        "verified": bool(r.get("verified")),
        "has_extracted": bool((extracted or "").strip()),
        "old_completeness": r.get("old_completeness"),
        "text_overlap": text_overlap(website, extracted),
        "_extracted": extracted,
        "_markdown": r.get("markdown"),
        "judge": None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="cap properties scanned (0 = all)")
    ap.add_argument("--judge-sample", type=int, default=0, help="run the LLM judge on N random rows")
    ap.add_argument("--csv", help="write per-property results to this CSV")
    ap.add_argument("--seed", type=int, default=0, help="random seed for the judge sample")
    args = ap.parse_args()

    cypher = FETCH + (f"\nLIMIT {int(args.limit)}" if args.limit else "")
    with read_session() as s:
        rows = [build_row(dict(r)) for r in s.run(cypher)]
    if not rows:
        print("No properties found.")
        return 1
    total = len(rows)

    # ── Cheap pass: text_overlap distribution ──
    buckets = {"0.0-0.5": 0, "0.5-0.7": 0, "0.7-0.85": 0, "0.85-1.0": 0}
    for r in rows:
        o = r["text_overlap"]
        key = "0.0-0.5" if o < 0.5 else "0.5-0.7" if o < 0.7 else "0.7-0.85" if o < 0.85 else "0.85-1.0"
        buckets[key] += 1
    extracted_n = sum(1 for r in rows if r["has_extracted"])
    print(f"\nScanned {total} properties ({extracted_n} have an extraction).\n")
    print("text_overlap (E vs eauctionsindia.com) distribution:")
    for k, v in buckets.items():
        print(f"  {k:>9}: {v:5d}  ({100 * v / total:4.1f}%)")

    # ── LLM judge sample ──
    if args.judge_sample:
        candidates = [r for r in rows if r["has_extracted"]]
        random.seed(args.seed)
        sample = random.sample(candidates, min(args.judge_sample, len(candidates)))
        print(f"\nRunning LLM judge on {len(sample)} sampled properties…")
        for r in sample:
            target = r["title"] or (r["borrowers"][0] if r["borrowers"] else "") or r["auction_id"]
            r["judge"] = run_judge(r["_markdown"], r["_extracted"], target)

        judged = [r for r in sample if r["judge"]]
        print(f"\njudged ok: {len(judged)}/{len(sample)}")
        for r in judged:
            j = r["judge"]
            print(
                f"  {r['auction_id']}: complete={j.get('complete')} "
                f"score={j.get('completeness')} conf={j.get('confidence')} "
                f"wrong={j.get('wrong_property')} overlap={r['text_overlap']} "
                f"missing={j.get('missing_parts')}"
            )
        print("\nauto-clear rate on judged sample (single, source!=human):")
        print(f"  {'comp_min':>8} {'conf_min':>8} {'auto':>6} {'%sample':>8}")
        for comp_min in (0.80, 0.85, 0.90):
            for conf_min in (0.75, 0.80):
                auto = sum(1 for r in judged if auto_verifiable(r, comp_min, conf_min))
                pct = 100 * auto / len(judged) if judged else 0
                print(f"  {comp_min:>8.2f} {conf_min:>8.2f} {auto:>6d} {pct:>7.1f}%")
    else:
        print("\n(no --judge-sample given; ran the cheap pre-filter only)")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            cols = ["auction_id", "notice_type", "source", "verified", "has_extracted",
                    "old_completeness", "text_overlap", "judge"]
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([r.get(c) if c != "judge" else json.dumps(r["judge"]) for c in cols])
        print(f"\nWrote per-property results to {args.csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
