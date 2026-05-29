"""
pipeline/score_markdown.py
--------------------------
Score every :Document.markdown for OCR quality, so the human reviewer in
web/review.html only has to inspect the low-scoring ones.

Blended quality score
~~~~~~~~~~~~~~~~~~~~~~~
The score is driven by how faithfully the OCR markdown reproduces the property
description we scraped from the source website — a long, specific probe that
can't match by coincidence the way a lone price can. The reserve price and a
borrower name act as corroborating bonuses.

For each AuctionProperty that carries a `website_description`:

    coverage = description_coverage(website_description, markdown)   # 0–100
    price    = 100 if the reserve price appears in the markdown else 0
    borrower = 100 if a borrower name appears in the markdown else 0
    property_score = 0.7*coverage + 0.2*price + 0.1*borrower

    Document score = mean(property_score over those properties)

Documents whose properties all lack a `website_description` end up with
`score = NULL` (unscored) — flagged separately in the review UI. The score is
display-only: reviewers eyeball it and bulk-confirm a range; nothing is
auto-adopted from it.

Usage
~~~~~
    python -m pipeline.score_markdown            # score only unscored docs
    python -m pipeline.score_markdown --force    # re-score everything
    python -m pipeline.score_markdown --limit 100

The function `score_freshly_loaded(file_paths)` is also imported by
pipeline/load_markdowns_to_neo4j.py to score newly-loaded Documents inline.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Iterable

from api.neo4j_client import run_query, run_read_query
from api.review.markdown_match import (
    borrower_in_markdown,
    description_coverage,
    price_in_markdown,
)

# Blend weights — description coverage dominates; price + borrower corroborate.
W_COVERAGE = 0.7
W_PRICE = 0.2
W_BORROWER = 0.1


def _fetch_docs(file_paths: list[str] | None, force: bool) -> list[dict]:
    """Pull Documents to score together with their linked properties.

    file_paths=None → all eligible Documents.
    force=False    → skip Documents that already have a score.
    """
    where = ["d.markdown IS NOT NULL", "d.markdown <> ''"]
    params: dict = {}
    if file_paths is not None:
        where.append("d.file_path IN $file_paths")
        params["file_paths"] = file_paths
    if not force:
        where.append("d.markdown_quality_score IS NULL")

    cypher = f"""
        MATCH (d:Document)
        WHERE {' AND '.join(where)}
        OPTIONAL MATCH (d)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
        OPTIONAL MATCH (a)-[:HAS_BORROWER]->(b:Borrower)
        WITH d, a, collect(DISTINCT b.name) AS borrowers
        WITH d, collect(CASE WHEN a IS NULL THEN NULL ELSE {{
                reserve_price:       a.reserve_price_num,
                website_description: a.website_description,
                borrowers:           borrowers
             }} END) AS props_raw
        RETURN d.file_path AS file_path,
               d.markdown  AS markdown,
               [p IN props_raw WHERE p IS NOT NULL] AS properties
    """
    return run_read_query(cypher, params, max_rows=20_000, timeout=60.0)


def _score_property(markdown: str, prop: dict) -> float | None:
    """Blended 0–100 score for one property, or None if it has no
    `website_description` to probe with."""
    website_desc = (prop.get("website_description") or "").strip()
    if not website_desc:
        return None
    coverage, _span = description_coverage(website_desc, markdown)
    price = 100.0 if price_in_markdown(prop.get("reserve_price"), markdown) else 0.0
    borrower = 100.0 if borrower_in_markdown(prop.get("borrowers"), markdown) else 0.0
    return W_COVERAGE * coverage + W_PRICE * price + W_BORROWER * borrower


def _score_one(markdown: str, properties: list[dict]) -> float | None:
    """Return the document's 0–100 blended score: the mean of the per-property
    scores over properties that carry a `website_description`. None (unscored)
    when no property has one."""
    if not markdown:
        return None
    scored = [s for s in (_score_property(markdown, p) for p in properties)
              if s is not None]
    if not scored:
        return None
    return round(sum(scored) / len(scored), 1)


def _write_scores(rows: list[dict]) -> None:
    """Persist {file_path, score} pairs (score may be None → unscored)."""
    if not rows:
        return
    run_query(
        """
        UNWIND $rows AS row
        MATCH (d:Document {file_path: row.file_path})
        SET d.markdown_quality_score    = row.score,
            d.markdown_quality_scored_at = datetime()
        """,
        {"rows": rows},
    )


def score_freshly_loaded(file_paths: Iterable[str]) -> int:
    """Compute and persist scores for the given file_paths. Returns count.

    Always re-scores (force=True), since the caller just wrote new markdown
    and any prior score is stale.
    """
    fps = [p for p in file_paths if p]
    if not fps:
        return 0
    docs = _fetch_docs(file_paths=fps, force=True)
    payload = [
        {"file_path": d["file_path"],
         "score": _score_one(d["markdown"] or "", d.get("properties") or [])}
        for d in docs
    ]
    _write_scores(payload)
    return len(payload)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="re-score Documents that already have a score")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N Documents (staged rollout)")
    ap.add_argument("--write-batch", type=int, default=500,
                    help="rows per Neo4j UNWIND write")
    args = ap.parse_args()

    print("Fetching Documents from Neo4j…")
    docs = _fetch_docs(file_paths=None, force=args.force)
    if args.limit:
        docs = docs[: args.limit]
    total = len(docs)
    print(f"Scoring {total} Documents (force={args.force})")
    if total == 0:
        return 0

    batch: list[dict] = []
    done = 0
    unscored = 0
    t0 = time.time()
    for d in docs:
        score = _score_one(d.get("markdown") or "", d.get("properties") or [])
        if score is None:
            unscored += 1
        batch.append({"file_path": d["file_path"], "score": score})
        done += 1

        if len(batch) >= args.write_batch:
            _write_scores(batch)
            batch = []

        if done % 500 == 0 or done == total:
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            print(f"  [{done}/{total}]  rate={rate:.0f}/s  unscored={unscored}",
                  flush=True)

    if batch:
        _write_scores(batch)

    print(f"\nScored {done} Documents  unscored={unscored}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
