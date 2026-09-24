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

  --only F   Re-extract exactly these notices (repeatable), whatever their
             date, OCR score or extraction state. For targeted repairs.

  --keep-more-lots
             Refuse a re-extraction that finds fewer lots than the stored one.
             A long notice's recall varies run to run, so without this a
             weaker run silently replaces a stronger one.

A multi-lot notice whose price lines match its confirmed lot count is read a
few lots at a time (pipeline/lot_chunks) — see that module for why.

  --refresh  Re-extract notices whose stored extraction no longer reflects its
             own inputs — the markdown was rewritten after the extraction ran,
             or it scored below --min-score. Add --single-lot to restrict to
             notices carrying exactly one :Lot. Unlike --stale this is computed
             from the timestamps rather than read off `extraction_stale_at`, so
             it also catches rewrites nothing stamped a flag for.

A re-extraction always returns the notice to `extraction_review_status =
'pending'` and drops `extraction_verified_by/at`: the entities a reviewer
verified are gone, so their verdict cannot stand over the new ones.

One page is extracted once. A notice published against several lots is stored as
several :Documents holding the same markdown, so they are grouped before any
model call (pipeline/notice_twins): one call per distinct text, the union of the
group's portal rosters passed to it, and the result written to every copy. In
the default resume mode a page another Document has already extracted is copied
rather than re-run; --no-resume and --stale turn that off, because re-running
the model is exactly what they are for.

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
from pipeline.extract_routing import (
    passes_for,
    select_extract_model,
    select_retry_model,
)
from pipeline.lot_chunks import extract_chunked, lots_read, plan_chunks
from pipeline.stitch_refresh import refresh_stitches
from pipeline.load_extractions import (
    ROSTER_CYPHER,
    _entities,
    _next_batch,
    _plan_groups,
)
from pipeline.validators import SCORE_VERSION, validate_stored

# A notice with at least this many lots is read in chunks from the start;
# a smaller one is read whole first (see read_notice).
CHUNK_FIRST_AT = int(os.getenv("LOT_CHUNK_FIRST_AT", "20"))

# Every LangExtract-owned field on :Document. Clearing these returns a notice to
# the "never extracted" state the /review/extraction surface treats as empty.
LANGEXTRACT_FIELDS = (
    "extraction_json",
    "extraction_score",
    "extraction_score_version",
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
        "d.stitched_into IS NULL",
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
        "RETURN d.filename AS filename, "
        "       coalesce(d.stitched_markdown, d.markdown) AS md, "
        "       d.notice_type AS notice_type, "
        "       coalesce(d.stitched_expected_lot_count, d.expected_lot_count) "
        "AS expected_lot_count, "
        "       roster AS roster "
        "ORDER BY d.filename"
        + (f" LIMIT {int(limit)}" if limit else "")
    )
    return run_read_query(
        q,
        {"since": f"{since}T00:00:00Z", "min_ocr": int(min_ocr)},
        max_rows=20_000, timeout=120.0)


def select_only_docs(filenames: list[str]) -> list[dict]:
    """Exactly the named Documents, in the same shape as the other selectors.
    No date, OCR or resume filter: naming a notice is the decision."""
    q = (
        "MATCH (d:Document) "
        "WHERE d.filename IN $fns "
        "  AND d.markdown IS NOT NULL AND d.markdown <> '' "
        "  AND d.stitched_into IS NULL "
        + ROSTER_CYPHER +
        "RETURN d.filename AS filename, "
        "       coalesce(d.stitched_markdown, d.markdown) AS md, "
        "       d.notice_type AS notice_type, "
        "       coalesce(d.stitched_expected_lot_count, d.expected_lot_count) "
        "AS expected_lot_count, "
        "       roster AS roster "
        "ORDER BY d.filename"
    )
    return run_read_query(q, {"fns": list(filenames)},
                          max_rows=20_000, timeout=120.0)


def _lot_count(ents: list[dict]) -> int:
    return len({str((e.get("attrs") or {}).get("lot_index"))
                for e in ents
                if (e.get("attrs") or {}).get("lot_index") not in (None, "")})


def _stored_lot_count(filename: str) -> int:
    rows = run_read_query(
        "MATCH (d:Document {filename: $fn}) RETURN d.extraction_json AS j",
        {"fn": filename})
    raw = rows[0]["j"] if rows else None
    try:
        return _lot_count(json.loads(raw)) if raw else 0
    except (TypeError, ValueError):
        return 0


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
        "  AND d.stitched_into IS NULL "
        "  AND d.ocr_health_score > $min_ocr "
        + ROSTER_CYPHER +
        "RETURN d.filename AS filename, "
        "       coalesce(d.stitched_markdown, d.markdown) AS md, "
        "       d.notice_type AS notice_type, "
        "       coalesce(d.stitched_expected_lot_count, d.expected_lot_count) "
        "AS expected_lot_count, "
        "       roster AS roster "
        "ORDER BY d.filename"
        + (f" LIMIT {int(limit)}" if limit else "")
    )
    return run_read_query(q, {"min_ocr": int(min_ocr)},
                          max_rows=20_000, timeout=120.0)


def select_refresh_docs(min_ocr: int, min_score: int, single_lot: bool,
                        limit: int | None, multi_lot: bool = False,
                        extracted_before: str | None = None,
                        unlinked: bool = False,
                        min_chars: int | None = None) -> list[dict]:
    """Documents whose stored extraction no longer reflects its own inputs.

    Two independent ways an extraction goes out of date without anything
    clearing it:

    * **the markdown was rewritten after it ran** — the notice text the model
      read is not the text on the node today, so the entities under-report (or
      mis-quote) the current source. `datalab: keep page headers and footers`
      (#425) rewrote 222 single-lot notices this way.
    * **the extraction scored below `min_score`** — the run itself failed to
      read the notice, regardless of what the markdown says. This compares
      stored scores against one threshold, so it assumes they are all on the
      current validators.py scale: run
      `python -m scripts.backfill_extraction_scores` after a SCORE_VERSION bump,
      or this signal selects a different set of notices depending on when each
      was last extracted.

    This is `select_stale_docs`'s condition computed from the timestamps rather
    than read off a flag: `extraction_stale_at` only exists where
    `scripts/fix_missing_regions.py` stamped it, and a markdown rewrite from any
    other source leaves no marker at all.

    ``single_lot`` restricts to notices carrying exactly one :Lot — the set
    where a listing takes its lot from the `single` rule in
    `apply_extractions.match_lots_to_listings`, so a re-extraction changes the
    fields and never the lot link. ``multi_lot`` is its complement (2+ lots),
    the only set where the lot match is decided by keys at all.

    ``extracted_before`` adds a third staleness signal, for the case the other
    two cannot see: the PROMPT changed, not the notice. An extraction older
    than the change was produced by a different question, so it is stale even
    though its markdown and score are untouched — `portal_aid` is exactly this
    (a lot extracted before it existed makes no claim, and the matcher has
    nothing to verify). Pass the change's timestamp; it ORs with the other two.

    ``unlinked`` narrows to notices that still have a listing with no
    `IS_LOT` edge — the ones a re-extraction can actually REPAIR, as opposed
    to merely re-check. 42 of 619 multi-lot notices are in that state against
    577 already fully linked, so this is the difference between a half-hour
    run and a six-hour one. It is an AND, not another staleness signal: a
    notice with nothing left to fix is not made urgent by being old.

    ``min_chars`` keeps only notices whose read text (the stitched text on a
    leader) is at least that long. With ``extracted_before`` it scopes a
    window-ceiling change to the notices the old window actually cut, instead
    of re-extracting the whole corpus.
    """
    if single_lot and multi_lot:
        raise ValueError("--single-lot and --multi-lot are mutually exclusive")
    lot_filter = ""
    if single_lot or multi_lot:
        op = "=" if single_lot else ">"
        lot_filter = ("MATCH (d)-[:HAS_LOT]->(l:Lot) "
                      f"WITH d, count(l) AS lots WHERE lots {op} 1 ")
    stale_when = "md > ex OR st > ex OR d.extraction_score < $min_score"
    if extracted_before:
        stale_when += " OR ex < $extracted_before"
    if unlinked:
        stale_when = (
            f"({stale_when}) AND EXISTS {{ "
            "MATCH (_a:AuctionProperty)-[:HAS_DOCUMENT]->(d) "
            "WHERE NOT (_a)-[:IS_LOT]->(:Lot) }")
    q = (
        "MATCH (d:Document) "
        "WHERE d.extraction_json IS NOT NULL "
        "  AND d.markdown IS NOT NULL AND d.markdown <> '' "
        "  AND d.stitched_into IS NULL "
        "  AND d.ocr_health_score > $min_ocr "
        + ("  AND size(coalesce(d.stitched_markdown, d.markdown)) >= $min_chars "
           if min_chars is not None else "")
        + lot_filter +
        "WITH d, toString(d.extraction_at) AS ex, "
        "     toString(coalesce(d.markdown_raw_at, d.markdown_loaded_at)) AS md, "
        "     toString(d.stitched_at) AS st "
        f"WHERE {stale_when} "
        + ROSTER_CYPHER +
        "RETURN d.filename AS filename, "
        "       coalesce(d.stitched_markdown, d.markdown) AS md, "
        "       d.notice_type AS notice_type, "
        "       coalesce(d.stitched_expected_lot_count, d.expected_lot_count) "
        "AS expected_lot_count, "
        "       roster AS roster "
        "ORDER BY d.filename"
        + (f" LIMIT {int(limit)}" if limit else "")
    )
    params = {"min_ocr": int(min_ocr), "min_score": int(min_score)}
    if extracted_before:
        params["extracted_before"] = extracted_before
    if min_chars is not None:
        params["min_chars"] = int(min_chars)
    return run_read_query(q, params, max_rows=20_000, timeout=120.0)


def read_notice(d: dict, route: bool) -> tuple[list[dict], str | None]:
    """Extract one page's entities without writing anything.

    Returns ``(entities, model_id)``. A multi-lot notice whose lots
    pipeline/lot_chunks can locate is read a few lots at a time; everything
    else is read whole. Shared by the writer below and by
    scripts/eval_lot_recall, so what the eval measures is what gets written."""
    from pipeline import langextract_examples as LX  # heavy import, defer
    if route:
        model_id, reasoning_off = select_extract_model(d.get("notice_type"))
    else:
        model_id, reasoning_off = None, False
    passes = passes_for(d.get("notice_type")) if route else None

    def read(text: str, lots, extra: str | None = None,
             strong: bool = False) -> list[dict]:
        mid, roff = ((select_retry_model() if route else (None, False))
                     if strong else (model_id, reasoning_off))
        res = LX.extract(text, model_id=mid, reasoning_off=roff,
                         expected_lot_count=lots, roster=d.get("roster"),
                         passes=passes, extra=extra)
        return _entities(res, text)

    # Chunked reading finds lots a whole read misses, but it is one model call
    # per chunk plus retries — several times the cost and 20–45 minutes on a
    # big notice. So: big notices go straight to chunks (a whole read of 25+
    # lots came back short every time it was tried — boi 18/25, tata 35/47,
    # L842); smaller ones are read whole and fall back to chunks only when
    # that read comes back short.
    expected = d.get("expected_lot_count")
    plan = plan_chunks(d["md"], expected)
    if plan is not None and int(expected) >= CHUNK_FIRST_AT:
        return extract_chunked(d["md"], plan, read), model_id
    ents = read(d["md"], expected)
    if plan is not None and lots_read(ents) < int(expected):
        print(f"  {d['filename']}: whole read found {lots_read(ents)} of "
              f"{expected} lot(s) — re-reading in chunks", flush=True)
        chunked = extract_chunked(d["md"], plan, read)
        if lots_read(chunked) >= lots_read(ents):
            return chunked, model_id
    return ents, model_id


def _extract_one(d: dict, batch: int, route: bool, keep_more_lots: bool = False):
    """Extract + write one page. Returns (filename, n_entities, model_id) on
    success or raises. Safe to call from a worker thread: LX.extract builds its
    own provider client per call and each write is an independent HTTP request.

    ``d`` may be a group leader from ``_plan_groups`` — one notice stored under
    several file names — in which case ``twins`` lists every Document the result
    is written to. They hold the same markdown, so the offsets in the entities
    are valid in each."""
    fn = d["filename"]
    targets = d.get("twins") or [fn]
    ents, model_id = read_notice(d, route)
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
    if keep_more_lots:
        new, old = _lot_count(ents), _stored_lot_count(fn)
        if new < old:
            raise ValueError(f"found {new} lot(s), stored extraction has "
                             f"{old} — keeping the existing one")
    # Scored from the entities that get stored (spans regrounded), so the
    # number describes the document a reader opens — see _extract_one.
    score = validate_stored(ents, source_text=d["md"])["score"]
    run_query(
        """
        UNWIND $fns AS name
        MATCH (d:Document {filename: name})
        SET d.extraction_json = $j,
            d.extraction_score = $score,
            d.extraction_score_version = $score_version,
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
            d.extraction_reused_from =
                CASE WHEN name = $fn THEN NULL ELSE $fn END,
            // Fresh entities now reflect the current markdown, so the staleness
            // marker fix_missing_regions left behind is cleared here — the flag
            // must not outlive the condition it describes.
            d.extraction_stale_at = NULL
        REMOVE d.extraction_verified_by, d.extraction_verified_at
        RETURN d.filename
        """,
        {"fn": fn, "fns": targets, "j": json.dumps(ents, ensure_ascii=False),
         "score": score, "score_version": SCORE_VERSION, "batch": batch})
    # New entities, new key-entity checklist (pipeline/key_entities.py).
    from pipeline.key_entities import stamp_key_scores
    stamp_key_scores(targets)
    return fn, len(ents), model_id or "default"


def extract_docs(docs: list[dict], concurrency: int = 1,
                 reuse: bool = True, keep_more_lots: bool = False) -> int:
    """Extract each page and write it as soon as it returns. Mirrors the write in
    pipeline.load_extractions.run so the review surface reads it unchanged.

    `concurrency` documents are processed in parallel (one worker thread each);
    every completed notice is persisted immediately, so an interrupted run keeps
    all finished work and --resume continues from there.

    Documents holding the same markdown are one page under several file names
    (pipeline/notice_twins): they are extracted once, with the union of their
    portal rosters, and the result written to each. With `reuse` a page some
    other Document has already extracted is copied instead of re-run — turned
    off for --no-resume and --stale, where re-running the model is the point.
    """
    if not docs:
        print("nothing to extract")
        return 0
    batch = _next_batch()
    docs, reused = _plan_groups(docs, force=not reuse, batch=batch)
    if reused:
        print(f"reused an existing extraction for {reused} document(s)")
    if not docs:
        print(f"nothing left to extract (batch B{batch})")
        return 0
    route = os.environ.get("LANGEXTRACT_PROVIDER", "openrouter").lower() == "openrouter"
    total = len(docs)
    covered = sum(len(d.get("twins") or [d["filename"]]) for d in docs)
    print(f"batch B{batch} — extracting {total} page(s) covering {covered} "
          f"document(s), concurrency={concurrency}")
    model_counts: Counter = Counter()
    lock = threading.Lock()
    ok = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(_extract_one, d, batch, route, keep_more_lots): d
                for d in docs}
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
    ap.add_argument("--multi-lot", action="store_true",
                    help="with --refresh, only notices carrying 2+ :Lots — "
                         "the set whose lot match is decided by keys")
    ap.add_argument("--extracted-before",
                    help="with --refresh, also treat an extraction older than "
                         "this ISO timestamp as stale (a prompt change the "
                         "markdown and score cannot see)")
    ap.add_argument("--unlinked", action="store_true",
                    help="with --refresh, only notices that still have a "
                         "listing with no IS_LOT edge — the ones a "
                         "re-extraction can repair rather than just re-check")
    ap.add_argument("--min-chars", type=int, default=None,
                    help="with --refresh, only notices whose text (stitched "
                         "text on a leader) is at least this many characters "
                         "— e.g. 30000 to redo just the notices the old "
                         "window cut")
    ap.add_argument("--only", action="append", default=None, metavar="FILENAME",
                    help="re-extract exactly this Document (repeatable); "
                         "ignores every other selector")
    ap.add_argument("--keep-more-lots", action="store_true",
                    help="keep the stored extraction when a re-run finds "
                         "fewer lots than it")
    ap.add_argument("--count-only", action="store_true",
                    help="print how many documents match and exit")
    args = ap.parse_args()

    since = args.since or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if args.clear or args.clear_only:
        clear_all()
        if args.clear_only:
            return 0

    # Read joined notices from their pages' current text, whatever rewrote a
    # page since the join. A rebuilt leader is stamped stale, so --stale takes it.
    rebuilt = refresh_stitches()
    if rebuilt:
        print(f"rebuilt joined text of {len(rebuilt)} notice(s): {', '.join(rebuilt)}")

    if args.only:
        docs = select_only_docs(args.only)
        print(f"matched {len(docs)} of {len(args.only)} named document(s)")
    elif args.refresh:
        docs = select_refresh_docs(args.min_ocr, args.min_score,
                                   args.single_lot, limit=args.limit,
                                   multi_lot=args.multi_lot,
                                   extracted_before=args.extracted_before,
                                   unlinked=args.unlinked,
                                   min_chars=args.min_chars)
        scope = ("single-lot " if args.single_lot
                 else "multi-lot " if args.multi_lot else "")
        older = (f", or extracted before {args.extracted_before}"
                 if args.extracted_before else "")
        only = " with an unlinked listing" if args.unlinked else ""
        print(f"matched {len(docs)} {scope}document(s){only} to refresh "
              f"(ocr>{args.min_ocr}; markdown rewritten since last extract, "
              f"or score < {args.min_score}{older})")
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
    # --stale and --no-resume both mean "run the model again", so neither may be
    # served a copy of an earlier extraction — but both still extract each page
    # once rather than once per file name.
    return extract_docs(docs, concurrency=max(1, args.concurrency),
                        reuse=not (args.stale or args.no_resume or args.only),
                        keep_more_lots=args.keep_more_lots)


if __name__ == "__main__":
    raise SystemExit(main())
