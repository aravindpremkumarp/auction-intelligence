"""Reset the LangExtract review corpus, then re-extract a targeted set of notices.

Two operations, gated independently so either can run alone:

  --clear    Wipe every LangExtract field off ALL :Document nodes so the
             /review/extraction surface starts empty. This removes the grounded
             entities AND the review state on top of them:
                 extraction_json  extraction_at  extraction_batch
                 extraction_review_status  extraction_corrections_json
                 extraction_verified_by  extraction_verified_at
             It is irreversible — reviewer corrections and verifications are lost.

  (extract) Run the canonical LangExtract pipeline
             (pipeline.langextract_examples.extract, per-notice-type model
             routing via pipeline.extract_routing) over every :Document that

               - has non-empty markdown,
               - has ocr_health_score > --min-ocr (default 90), and
               - backs at least one :AuctionProperty whose auction_start_dt is
                 on/after --since (default: today, UTC),

             writing the grounded entities back exactly as pipeline.load_extractions
             does (extraction_json / extraction_score / extraction_at /
             extraction_batch / extraction_review_status='pending'), all under one
             shared batch number.

  --refresh  Re-extract notices whose stored extraction no longer reflects its
             own inputs — the markdown was rewritten after the extraction ran,
             or it scored below --min-score. Add --single-lot to restrict to
             notices carrying exactly one :Lot. Unlike --stale this is computed
             from the timestamps rather than read off `extraction_stale_at`, so
             it also catches rewrites nothing stamped a flag for.

A re-extraction always returns the notice to `extraction_review_status =
'pending'` and drops `extraction_verified_by/at`: the entities a reviewer
verified are gone, so their verdict cannot stand over the new ones.

Each document is written the moment its extraction returns, so a run that is
interrupted leaves every completed notice persisted. Re-running with --resume
(the default) skips any :Document that already has extraction_json, so an
interrupted run simply continues where it stopped.

Run (HTTP egress, matches the rest of the pipeline in a web session):

    NEO4J_HTTP_API=1 LANGEXTRACT_PROVIDER=openrouter \
        python -m scripts.reset_langextract_and_extract --clear

    NEO4J_HTTP_API=1 LANGEXTRACT_PROVIDER=openrouter \
        python -m scripts.reset_langextract_and_extract --since 2026-07-23 --min-ocr 90
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from api.neo4j_client import run_query, run_read_query
from pipeline.extract_routing import select_extract_model
from pipeline.load_extractions import ROSTER_CYPHER, _entities, _next_batch
from pipeline.validators import validate

# Every LangExtract-owned field on :Document. Clearing these returns a notice to
# the "never extracted" state the /review/extraction surface treats as empty.
LANGEXTRACT_FIELDS = (
    "extraction_json",
    "extraction_score",
    "extraction_at",
    "extraction_batch",
    "extraction_review_status",
    "extraction_corrections_json",
    "extraction_verified_by",
    "extraction_verified_at",
)


def clear_all() -> int:
    """REMOVE every LangExtract field from all :Document nodes. Returns the count
    of documents that had an extraction before the wipe (for reporting)."""
    had = run_read_query(
        "MATCH (d:Document) WHERE d.extraction_json IS NOT NULL "
        "RETURN count(d) AS c")[0]["c"]
    removes = ", ".join(f"d.{f}" for f in LANGEXTRACT_FIELDS)
    run_query(f"MATCH (d:Document) REMOVE {removes}")
    print(f"cleared LangExtract state on all documents "
          f"({had} had an extraction)")
    return had


def select_docs(since: str, min_ocr: int, resume: bool,
                limit: int | None) -> list[dict]:
    """Distinct :Documents to extract: OCR health above threshold and backing an
    auction starting on/after `since`. `since` is an ISO date (YYYY-MM-DD)."""
    where = [
        "d.markdown IS NOT NULL AND d.markdown <> ''",
        "d.ocr_health_score > $min_ocr",
        "a.auction_start_dt >= datetime($since)",
    ]
    if resume:
        where.append("d.extraction_json IS NULL")
    q = (
        "MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document) "
        f"WHERE {' AND '.join(where)} "
        "WITH DISTINCT d "
        + ROSTER_CYPHER +
        "RETURN d.filename AS filename, d.markdown AS md, "
        "       d.notice_type AS notice_type, "
        "       d.expected_lot_count AS expected_lot_count, "
        "       roster AS roster "
        "ORDER BY d.filename"
        + (f" LIMIT {int(limit)}" if limit else "")
    )
    return run_read_query(
        q,
        {"since": f"{since}T00:00:00Z", "min_ocr": int(min_ocr)},
        max_rows=20_000, timeout=120.0)


def select_stale_docs(min_ocr: int, limit: int | None) -> list[dict]:
    """Documents whose markdown was rewritten after they were last extracted.

    ``scripts/fix_missing_regions.py`` stamps ``extraction_stale_at`` when it
    recovers a dropped region: the stored entities were read off text that was
    missing content, so they under-report the notice. These are exactly the
    documents ``--resume`` must NOT skip — they already have
    ``extraction_json``, and that is the problem rather than the reason to leave
    them alone.

    ``min_ocr`` still applies: a notice whose region could only be partly
    recovered is not worth spending an extraction on yet.
    """
    q = (
        "MATCH (d:Document) "
        "WHERE d.extraction_stale_at IS NOT NULL "
        "  AND d.markdown IS NOT NULL AND d.markdown <> '' "
        "  AND d.ocr_health_score > $min_ocr "
        + ROSTER_CYPHER +
        "RETURN d.filename AS filename, d.markdown AS md, "
        "       d.notice_type AS notice_type, "
        "       d.expected_lot_count AS expected_lot_count, "
        "       roster AS roster "
        "ORDER BY d.filename"
        + (f" LIMIT {int(limit)}" if limit else "")
    )
    return run_read_query(q, {"min_ocr": int(min_ocr)},
                          max_rows=20_000, timeout=120.0)


def select_refresh_docs(min_ocr: int, min_score: int, single_lot: bool,
                        limit: int | None) -> list[dict]:
    """Documents whose stored extraction no longer reflects its own inputs.

    Two independent ways an extraction goes out of date without anything
    clearing it:

    * **the markdown was rewritten after it ran** — the notice text the model
      read is not the text on the node today, so the entities under-report (or
      mis-quote) the current source. `datalab: keep page headers and footers`
      (#425) rewrote 222 single-lot notices this way.
    * **the extraction scored below `min_score`** — the run itself failed to
      read the notice, regardless of what the markdown says.

    This is `select_stale_docs`'s condition computed from the timestamps rather
    than read off a flag: `extraction_stale_at` only exists where
    `scripts/fix_missing_regions.py` stamped it, and a markdown rewrite from any
    other source leaves no marker at all.

    ``single_lot`` restricts to notices carrying exactly one :Lot — the set
    where a listing takes its lot from the `single` rule in
    `apply_extractions.match_lots_to_listings`, so a re-extraction changes the
    fields and never the lot link.
    """
    lot_filter = (
        "MATCH (d)-[:HAS_LOT]->(l:Lot) WITH d, count(l) AS lots WHERE lots = 1 "
        if single_lot else "")
    q = (
        "MATCH (d:Document) "
        "WHERE d.extraction_json IS NOT NULL "
        "  AND d.markdown IS NOT NULL AND d.markdown <> '' "
        "  AND d.ocr_health_score > $min_ocr "
        + lot_filter +
        "WITH d, toString(d.extraction_at) AS ex, "
        "     toString(coalesce(d.markdown_raw_at, d.markdown_loaded_at)) AS md "
        "WHERE md > ex OR d.extraction_score < $min_score "
        + ROSTER_CYPHER +
        "RETURN d.filename AS filename, d.markdown AS md, "
        "       d.notice_type AS notice_type, "
        "       d.expected_lot_count AS expected_lot_count, "
        "       roster AS roster "
        "ORDER BY d.filename"
        + (f" LIMIT {int(limit)}" if limit else "")
    )
    return run_read_query(q, {"min_ocr": int(min_ocr),
                              "min_score": int(min_score)},
                          max_rows=20_000, timeout=120.0)


def _extract_one(d: dict, batch: int, route: bool):
    """Extract + write one document. Returns (filename, n_entities, model_id) on
    success or raises. Safe to call from a worker thread: LX.extract builds its
    own provider client per call and each write is an independent HTTP request."""
    from pipeline import langextract_examples as LX  # heavy import, defer
    fn = d["filename"]
    if route:
        model_id, reasoning_off = select_extract_model(d.get("notice_type"))
    else:
        model_id, reasoning_off = None, False
    res = LX.extract(d["md"], model_id=model_id, reasoning_off=reasoning_off,
                     expected_lot_count=d.get("expected_lot_count"),
                     roster=d.get("roster"))
    ents = _entities(res)
    # An empty result is a failed read, not a notice with nothing in it — the
    # model returned something LangExtract could not parse ("Content must
    # contain an 'extractions' key"), and every chunk was skipped. Writing it
    # would replace a notice's entities with nothing, and on a re-extraction
    # that means DESTROYING the ones already there. Raise instead: the caller
    # counts a failure, the document keeps what it had, and the next run picks
    # it up again.
    if not ents:
        raise ValueError("extraction returned no entities — keeping the "
                         "existing one")
    score = validate(res.extractions, source_text=d["md"])["score"]
    run_query(
        """
        MATCH (d:Document {filename:$fn})
        SET d.extraction_json = $j,
            d.extraction_score = $score,
            d.extraction_at    = datetime(),
            d.extraction_batch = $batch,
            // A verification is a statement about entities a person actually
            // read. These entities are new, so the old verdict cannot cover
            // them: carrying `verified` forward (what
            // `coalesce(status,'pending')` used to do here) leaves a human's
            // name on rows nobody has seen, and the review queue reports a
            // notice as done when it is not. Back to 'pending' — the queue is
            // longer, and it is true.
            d.extraction_review_status = 'pending',
            // Fresh entities now reflect the current markdown, so the staleness
            // marker fix_missing_regions left behind is cleared here — the flag
            // must not outlive the condition it describes.
            d.extraction_stale_at = NULL
        REMOVE d.extraction_verified_by, d.extraction_verified_at
        RETURN d.filename
        """,
        {"fn": fn, "j": json.dumps(ents, ensure_ascii=False),
         "score": score, "batch": batch})
    return fn, len(ents), model_id or "default"


def extract_docs(docs: list[dict], concurrency: int = 1) -> int:
    """Extract each doc and write it as soon as it returns. Mirrors the write in
    pipeline.load_extractions.run so the review surface reads it unchanged.

    `concurrency` documents are processed in parallel (one worker thread each);
    every completed notice is persisted immediately, so an interrupted run keeps
    all finished work and --resume continues from there."""
    if not docs:
        print("nothing to extract")
        return 0
    batch = _next_batch()
    route = os.environ.get("LANGEXTRACT_PROVIDER", "openrouter").lower() == "openrouter"
    total = len(docs)
    print(f"batch B{batch} — extracting {total} document(s), concurrency={concurrency}")
    model_counts: Counter = Counter()
    lock = threading.Lock()
    ok = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(_extract_one, d, batch, route): d for d in docs}
        done = 0
        for fut in as_completed(futs):
            d = futs[fut]
            done += 1
            try:
                fn, n_ents, model_id = fut.result()
            except Exception as e:  # one bad doc must not stop the batch
                with lock:
                    fail += 1
                print(f"  [{done}/{total}] [fail] {d['filename']}: {e}", flush=True)
                continue
            with lock:
                ok += 1
                model_counts[model_id] += 1
                rate = (time.time() - t0) / ok
                eta = rate * (total - done)
            print(f"  [{done}/{total}] {fn}: {n_ents} entities "
                  f"(eta {eta/60:.0f}m)", flush=True)
    routing = "  ".join(f"{m}={n}" for m, n in sorted(model_counts.items()))
    print(f"model routing: {routing}")
    print(f"done — wrote {ok}, failed {fail} (batch B{batch}) "
          f"in {(time.time()-t0)/60:.1f}m")
    return 0 if fail == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clear", action="store_true",
                    help="wipe LangExtract state on ALL documents first")
    ap.add_argument("--clear-only", action="store_true",
                    help="clear and stop (no extraction)")
    ap.add_argument("--since", default=None,
                    help="only notices whose auction_start_dt is on/after this "
                         "ISO date (default: today, UTC)")
    ap.add_argument("--min-ocr", type=int, default=90,
                    help="ocr_health_score strictly greater than this (default 90)")
    ap.add_argument("--no-resume", action="store_true",
                    help="re-extract even documents that already have extraction_json")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=1,
                    help="documents to extract in parallel (default 1)")
    ap.add_argument("--stale", action="store_true",
                    help="re-extract Documents whose markdown was rewritten "
                         "after their last extraction (extraction_stale_at set "
                         "by scripts.fix_missing_regions); ignores --since and "
                         "--resume, since these already have extraction_json")
    ap.add_argument("--refresh", action="store_true",
                    help="re-extract Documents whose stored extraction no "
                         "longer reflects its inputs: markdown rewritten after "
                         "the last extraction, or extraction_score below "
                         "--min-score. Computed from timestamps, so it catches "
                         "rewrites --stale's flag never marked; ignores "
                         "--since and --resume")
    ap.add_argument("--min-score", type=int, default=60,
                    help="with --refresh, extraction_score below this counts "
                         "as needing a re-extraction (default 60)")
    ap.add_argument("--single-lot", action="store_true",
                    help="with --refresh, only notices carrying exactly one "
                         ":Lot")
    ap.add_argument("--count-only", action="store_true",
                    help="print how many documents match and exit")
    args = ap.parse_args()

    since = args.since or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if args.clear or args.clear_only:
        clear_all()
        if args.clear_only:
            return 0

    if args.refresh:
        docs = select_refresh_docs(args.min_ocr, args.min_score,
                                   args.single_lot, limit=args.limit)
        scope = "single-lot " if args.single_lot else ""
        print(f"matched {len(docs)} {scope}document(s) to refresh "
              f"(ocr>{args.min_ocr}; markdown rewritten since last extract, "
              f"or score < {args.min_score})")
    elif args.stale:
        docs = select_stale_docs(args.min_ocr, limit=args.limit)
        print(f"matched {len(docs)} document(s) with stale extractions "
              f"(ocr>{args.min_ocr}; markdown rewritten since last extract)")
    else:
        docs = select_docs(since, args.min_ocr, resume=not args.no_resume,
                           limit=args.limit)
        print(f"matched {len(docs)} document(s) "
              f"(ocr>{args.min_ocr}, auction_start >= {since}, "
              f"resume={not args.no_resume})")
    if args.count_only:
        return 0
    return extract_docs(docs, concurrency=max(1, args.concurrency))


if __name__ == "__main__":
    raise SystemExit(main())
