"""Populate Document.extraction_json for the /review/extraction review surface.

Runs the canonical LangExtract pipeline (pipeline/langextract_examples.extract)
over each Document's markdown and writes the grounded entities back to Neo4j as a
compact JSON array — the shape api/review/extraction.py reads:

  extraction_json = [{id, cls, text, start, end, attrs}, ...]

Also stamps ``extraction_model`` — the model that actually produced the
extraction, resolved through the same per-notice-type routing LX.extract uses.
Without it a score change cannot be attributed to a model change, since the
routing config is env/code state that is not recoverable after the fact.

Idempotent-ish: by default skips Documents that already have extraction_json
(use --force to re-extract). Review state (corrections / verified) is preserved.

Documents are extracted concurrently (--workers, default 8) — each one is a
single multi-minute model call, so a serial run over the full corpus is days of
wall time.

Run:  NEO4J_HTTP_API=1 LANGEXTRACT_PROVIDER=openrouter \
        python -m pipeline.load_extractions --limit 50 --workers 24
"""
from __future__ import annotations

import argparse
import json
import os
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from api.neo4j_client import run_query, run_read_query
from pipeline.extract_routing import select_extract_model
from pipeline.validators import normalize_identifier_kind, validate

# Documents run concurrently (see run()). Sized for a provider rate limit, not
# CPU — each worker spends its time waiting on one model call.
DEFAULT_WORKERS = int(os.environ.get("LOAD_EXTRACTIONS_WORKERS", "8"))

# pipeline.langextract_examples is imported lazily inside run() — it pulls the
# heavy `langextract` dependency, which isn't needed to import this module for
# the batch-numbering helper (or its unit tests).


# The portal's own row per lot, scraped independently of the notice image, fed
# to the prompt as segmentation context (see
# langextract_examples.portal_roster_block). Shared with
# scripts/reset_langextract_and_extract.py so both extraction entry points send
# the model the same context — a notice must not be extracted differently
# depending on which script ran it. Expects `d` in scope and yields `roster`;
# rows with nothing usable are dropped here so the renderer sees a clean list.
ROSTER_CYPHER = (
    "OPTIONAL MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d) "
    # aid keeps rows unique — without it DISTINCT collapses two listings whose
    # visible fields happen to match, real on notices that sell several
    # identical flats, and the roster would under-report its own length. It is
    # also rendered now: the model names the listing a lot belongs to by
    # quoting this id back as `portal_aid`, which is what turns the roster from
    # a segmentation hint into an assignment the matcher can verify.
    #
    # borrower and desc are the two fields that separate lots money cannot:
    # sibling flats in one building share a reserve price, an EMD and a village
    # but not their borrower or the portal's own one-line description.
    "WITH d, [row IN collect(DISTINCT {"
    "     aid: a.auction_id, "
    "     reserve: a.reserve_price_num, emd: a.emd_num, "
    "     village: a.village, district: a.district, "
    "     area: a.total_area, ptype: a.property_type_norm, "
    "     borrower: head([(a)-[:HAS_BORROWER]->(bo) | bo.name]), "
    "     desc: a.website_description}) "
    "  WHERE row.reserve IS NOT NULL OR row.emd IS NOT NULL "
    "     OR row.village IS NOT NULL] AS roster "
)


def _fetch(limit: int | None, force: bool, filename: str | None) -> list[dict]:
    where = "d.markdown IS NOT NULL AND d.markdown <> ''"
    if not force:
        where += " AND d.extraction_json IS NULL"
    if filename:
        where += " AND d.filename = $fn"
    return run_read_query(
        f"MATCH (d:Document) WHERE {where} "
        + ROSTER_CYPHER +
        "RETURN d.filename AS filename, d.markdown AS md, "
        "       d.notice_type AS notice_type, "
        "       d.expected_lot_count AS expected_lot_count, "
        "       roster AS roster "
        "ORDER BY d.filename"
        + (f" LIMIT {int(limit)}" if limit else ""),
        {"fn": filename} if filename else None,
        max_rows=20_000, timeout=120.0)


#: Misspellings of a schema class, mapped to the class the model meant.
#:
#: A class the dispatch does not know is not an error anywhere — `cls` is read
#: through if/elif chains in `apply_extractions` and `promote_extractions` with
#: no fallback, so an unrecognised one matches nothing and is silently skipped.
#: One notice spelled `borrower` with a missing r on 15 entities and lost every
#: one of them: read correctly, grounded to real spans, and dropped on the
#: floor for a typo.
#:
#: Only unambiguous misspellings belong here. A class that is genuinely outside
#: the schema (`extraction_text`, on 3 documents) is NOT guessed at — its
#: content is real but its intended class is not recoverable from the label, so
#: it stays as the model emitted it for a human to look at.
_CLASS_ALIASES = {
    "borower": "borrower",
}


def _entities(res) -> list[dict]:
    out = []
    for i, e in enumerate(res.extractions):
        ci = getattr(e, "char_interval", None)
        attrs = dict(e.attributes or {})
        cls = e.extraction_class
        # Same shape as the identifier-kind normalisation below: correct the
        # value, keep the original beside it so review sees what was said.
        if cls in _CLASS_ALIASES:
            attrs["cls_raw"] = cls
            cls = _CLASS_ALIASES[cls]
        # Safety net: models sometimes copy the document's label as the
        # identifier kind ("T.S.No") instead of the enum — normalize, keeping
        # the original in kind_raw so nothing is lost for review.
        if cls == "identifier" and attrs.get("kind"):
            kind, changed = normalize_identifier_kind(attrs["kind"])
            if changed:
                attrs["kind_raw"] = attrs["kind"]
                attrs["kind"] = kind
        out.append({
            "id": str(i),
            "cls": cls,
            "text": e.extraction_text,
            "start": getattr(ci, "start_pos", None) if ci else None,
            "end": getattr(ci, "end_pos", None) if ci else None,
            "attrs": attrs,
        })
    return out


def _next_batch() -> int:
    """Next global extraction batch number (B1, B2, …). Every document written by
    a single `run()` shares this number, so the review queue can tag a run and the
    reviewer can tell at a glance which notices came from the latest re-extraction."""
    rows = run_read_query(
        "MATCH (d:Document) WHERE d.extraction_batch IS NOT NULL "
        "RETURN max(d.extraction_batch) AS m LIMIT 1")
    cur = rows[0]["m"] if rows else None
    return int(cur) + 1 if cur is not None else 1


def _effective_model(model_id: str | None, route: bool) -> str:
    """The model LX.extract will actually use, for provenance stamping.

    ``select_extract_model`` returns None on the gemini-direct path (and could
    in principle return None on the OpenRouter path), in which case
    ``langextract_examples.extract`` falls back to LANGEXTRACT_MODEL_ID — with a
    different default per provider. Mirror that resolution here so
    ``extraction_model`` records the model that ran, never None.

    The OpenRouter default tracks OPENROUTER_MODEL_EXTRACT_SINGLE rather than
    naming a model here: this function's whole job is to record what actually
    ran, and a second copy of the default is a second thing to forget to change.
    The gemini-direct branch keeps its own default because that path does not go
    through OpenRouter and its slugs are not interchangeable.
    """
    from pipeline.config import OPENROUTER_MODEL_EXTRACT_SINGLE
    if model_id:
        return model_id
    default = OPENROUTER_MODEL_EXTRACT_SINGLE if route else "gemini-2.5-flash"
    return os.environ.get("LANGEXTRACT_MODEL_ID", default)


def _extract_one(d: dict, batch: int, route: bool, LX) -> tuple[bool, str | None, str]:
    """Extract + persist one Document. Returns (ok, model_id, log_line).

    Runs on a worker thread: every document is independent (its own model call
    and its own single-row write), and `api.neo4j_client` hands out a fresh
    session per `run_query`, so nothing is shared but the driver — which the
    Neo4j driver documents as thread-safe.
    """
    fn = d["filename"]
    if route:
        model_id, reasoning_off = select_extract_model(d.get("notice_type"))
    else:
        model_id, reasoning_off = None, False
    effective_model = _effective_model(model_id, route)
    try:
        res = LX.extract(d["md"], model_id=model_id, reasoning_off=reasoning_off,
                         expected_lot_count=d.get("expected_lot_count"),
                         roster=d.get("roster"))
    except Exception as e:  # keep going; one bad doc shouldn't stop the load
        return False, model_id, f"[fail] {fn}: {e}"
    ents = _entities(res)
    # Label-free quality score (0-100, see pipeline/validators.py) — lets the
    # review queue surface low-quality extractions first via score_min/max.
    score = validate(res.extractions, source_text=d["md"])["score"]
    run_query(
        """
        MATCH (d:Document {filename:$fn})
        SET d.extraction_json = $j,
            d.extraction_score = $score,
            d.extraction_at    = datetime(),
            d.extraction_batch = $batch,
            d.extraction_model = $model,
            d.extraction_review_status =
                coalesce(d.extraction_review_status, 'pending')
        RETURN d.filename
        """,
        {"fn": fn, "j": json.dumps(ents, ensure_ascii=False),
         "score": score, "batch": batch, "model": effective_model})
    return True, model_id, (f"{fn}: {len(ents)} fields, score={score}, "
                            f"model={effective_model}")


def run(limit: int | None, force: bool, filename: str | None,
        workers: int = DEFAULT_WORKERS) -> int:
    from pipeline import langextract_examples as LX
    docs = _fetch(limit, force, filename)
    print(f"to extract: {len(docs)} document(s)")
    if not docs:
        print("done — wrote 0, failed 0")
        return 0
    batch = _next_batch()
    workers = max(1, min(workers, len(docs)))
    print(f"batch B{batch} — {workers} worker(s)")
    # Per-notice-type model routing applies on the OpenRouter path only; the
    # gemini-direct path keeps its single env-configured model.
    route = os.environ.get("LANGEXTRACT_PROVIDER", "openrouter").lower() == "openrouter"
    model_counts: Counter = Counter()
    ok = fail = done = 0
    # Documents are extracted concurrently because each one is a single
    # multi-minute model call and LangExtract's own max_workers only splits a
    # document across windows — and char_buffer_for sizes the window to the whole
    # notice, so almost every document is one window and those workers idle.
    # Serial, the full corpus is days of wall time; the ceiling here is the
    # provider's rate limit, not local CPU.
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_extract_one, d, batch, route, LX): d for d in docs}
        for fut in as_completed(futures):
            d = futures[fut]
            try:
                good, model_id, line = fut.result()
            except Exception as e:  # a crash in the worker itself, not the model
                good, model_id, line = False, None, f"[error] {d['filename']}: {e}"
            with lock:
                done += 1
                if good:
                    ok += 1
                else:
                    fail += 1
                model_counts[model_id or "default"] += 1
                print(f"  [{done}/{len(docs)}] {line}", flush=True)
    if route:
        routing = "  ".join(f"{m}={n}" for m, n in sorted(model_counts.items()))
        print(f"model routing: {routing}")
    print(f"done — wrote {ok}, failed {fail} (batch B{batch})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true", help="re-extract even if present")
    ap.add_argument("--filename", help="only this Document.filename")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"documents extracted concurrently (default {DEFAULT_WORKERS}, "
                         "env LOAD_EXTRACTIONS_WORKERS)")
    args = ap.parse_args()
    return run(args.limit, args.force, args.filename, args.workers)


if __name__ == "__main__":
    raise SystemExit(main())
