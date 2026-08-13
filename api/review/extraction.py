"""
api/review/extraction.py
------------------------
Admin-only `/review/extraction/*` endpoints — the editable, grounded review
surface for LangExtract structured output (see pipeline/langextract_examples.py).

Mirrors the existing description/markdown review idiom (api/review/router.py +
queries.py): read a document's extraction, let a reviewer correct individual
fields, then verify/unverify — all written back to Neo4j with by-email + status
flags so corrections are auditable and can grow the eval gold set.

Data model (on :Document, populated by pipeline/load_extractions.py):
  extraction_json              JSON [{id, cls, text, start, end, attrs}]
  extraction_corrections_json  JSON {field_id: {value, by, at, notes}}
  extraction_review_status     'pending' | 'edited' | 'verified'
  extraction_verified_by / _at

This module is intentionally self-contained (its own APIRouter) so it can be
mounted alongside the main review router without touching the large queries.py.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.auth.dependencies import get_current_admin
from api.auth.schemas import UserOut
from api.neo4j_client import run_query, run_read_query
from api.review.queries import _date_exists_clause, _notice_type_clause

router = APIRouter(prefix="/review/extraction", tags=["review-extraction"])


# ── models ───────────────────────────────────────────────────────────────────
class ExtractionField(BaseModel):
    id: str
    cls: str
    text: str
    start: int | None = None
    end: int | None = None
    grounded: bool = True
    lot_index: str | None = None
    attrs: dict = {}
    corrected_value: str | None = None
    corrected_by: str | None = None
    corrected_at: str | None = None


class ExtractionReviewOut(BaseModel):
    filename: str
    markdown: str | None = None
    status: str = "pending"
    # Label-free quality score (0-100, pipeline/validators.py). None for
    # extractions written before scoring was tracked.
    score: int | None = None
    verified_by: str | None = None
    verified_at: str | None = None
    # Source-notice location so the review UI can show the original document
    # next to the markdown (Document props set by scripts/upload_downloads_to_r2).
    public_url: str | None = None
    doc_type: str | None = None  # "image" | "pdf" | "other"
    content_type: str | None = None
    # True when the markdown was re-ingested AFTER this extraction ran, so the
    # stored fields (and their char offsets) no longer match the source text —
    # the reviewer should re-run LangExtract (the ▶ button, or
    # load_extractions --filename --force from a shell).
    stale: bool = False
    # A POST /rerun is in flight for this document; poll the detail endpoint
    # until this clears, then re-render. rerun_error carries the last rerun's
    # failure so the reviewer sees why nothing changed.
    rerun_running: bool = False
    rerun_error: str | None = None
    fields: list[ExtractionField] = []


class ExtractionQueueRow(BaseModel):
    filename: str
    status: str
    n_fields: int
    n_ungrounded: int
    # Label-free quality score (0-100, pipeline/validators.py). None for rows
    # extracted before scoring was tracked.
    score: int | None = None
    # When load_extractions.py last wrote this document's extraction (ISO string,
    # None for rows extracted before this was tracked). Lets the review UI sort
    # newest-first so a freshly re-run batch clusters at the top of the queue.
    extraction_at: str | None = None
    # Which extraction run produced this document — every doc from one
    # load_extractions.py invocation shares this number (rendered "B7" in the UI).
    # None for rows extracted before batches were tracked.
    extraction_batch: int | None = None
    # Markdown re-ingested after this extraction ran -> a re-run is required.
    stale: bool = False
    # Reviewer's lot count from the classification gate (Document.
    # expected_lot_count) vs distinct lot_index values in this extraction.
    # mismatch=True is the checksum firing: LangExtract missed lots or
    # invented extras. Either count None -> mismatch stays False (no claim).
    expected_lot_count: int | None = None
    extracted_lot_count: int | None = None
    lot_count_mismatch: bool = False


class ExtractionQueueOut(BaseModel):
    rows: list[ExtractionQueueRow] = []
    total: int = 0


class FieldEditBody(BaseModel):
    field_id: str = Field(min_length=1, max_length=200)
    value: str = Field(max_length=20000)
    notes: str | None = Field(default=None, max_length=2000)


class ExtractionVerifyBody(BaseModel):
    notes: str | None = Field(default=None, max_length=2000)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def extraction_stale(md_reextracted_at: str | None, md_loaded_at: str | None,
                     extraction_at: str | None) -> bool:
    """True when the markdown changed after LangExtract last ran on it.

    Re-ingest stamps one of two markers depending on the path — a full MinerU
    re-ingest sets ``markdown_loaded_at``, a single-block re-OCR sets
    ``markdown_reextracted_at`` — so the extraction is stale when EITHER is newer
    than ``extraction_at``. All three are Neo4j datetimes rendered as ISO-8601
    UTC strings, which compare correctly lexicographically. Unknown extraction
    time (legacy rows) -> not stale (nothing to compare against)."""
    if not extraction_at:
        return False
    return any(t and t > extraction_at
               for t in (md_reextracted_at, md_loaded_at))


# ── query helpers (kept here so queries.py is untouched) ──────────────────────
def get_extraction(filename: str) -> dict | None:
    rows = run_read_query(
        """
        MATCH (d:Document {filename: $fn})
        WHERE d.extraction_json IS NOT NULL
        RETURN d.filename                                   AS filename,
               d.markdown                                   AS markdown,
               d.extraction_json                            AS extraction_json,
               coalesce(d.extraction_corrections_json, '{}') AS corrections_json,
               coalesce(d.extraction_review_status, 'pending') AS status,
               d.extraction_score                           AS score,
               d.extraction_verified_by                     AS verified_by,
               toString(d.extraction_verified_at)           AS verified_at,
               d.public_url                                 AS public_url,
               d.doc_type                                   AS doc_type,
               d.content_type                               AS content_type,
               toString(d.extraction_at)                    AS extraction_at,
               toString(d.markdown_reextracted_at)          AS markdown_reextracted_at,
               toString(d.markdown_loaded_at)               AS markdown_loaded_at
        LIMIT 1
        """,
        {"fn": filename},
    )
    return rows[0] if rows else None


def _extraction_filter_clause(
    status: str | None,
    score_min: float | None,
    score_max: float | None,
    notice_type: str | None,
    date_from: str | None,
    date_to: str | None,
    q: str | None,
) -> str:
    """Shared WHERE tail for the extraction queue and its stats.

    Keeping list + stats on one clause builder stops the header pills from
    counting a different set than the queue shows — the same trap
    ``_classification_where`` exists to avoid on the other stages. The
    notice_type / date helpers are imported from ``queries`` rather than
    re-written so every stage filters a notice identically.
    """
    clause = "AND coalesce(d.extraction_review_status,'pending') = $status" if status else ""
    # Rows without a score (pre-scoring extractions) are excluded once either
    # bound narrows the default 0-100 range — nothing to compare against.
    if score_min is not None:
        clause += " AND d.extraction_score >= $score_min"
    if score_max is not None:
        clause += " AND d.extraction_score <= $score_max"
    nt = _notice_type_clause(notice_type, alias="d")
    if nt:
        clause += f" AND {nt}"
    dt = _date_exists_clause(date_from, date_to, alias="d")
    if dt:
        clause += f" AND {dt}"
    if q:
        # Filename OR any linked listing's title/borrower, so the one search box
        # works whether the reviewer remembers the file or the property.
        clause += (
            " AND (toLower(coalesce(d.filename, '')) CONTAINS toLower($q)"
            " OR EXISTS { MATCH (d)<-[:HAS_DOCUMENT]-(_p:AuctionProperty)"
            "   WHERE toLower(coalesce(_p.title, '')) CONTAINS toLower($q) }"
            " OR EXISTS { MATCH (d)<-[:HAS_DOCUMENT]-(:AuctionProperty)"
            "   -[:HAS_BORROWER]->(_b:Borrower)"
            "   WHERE toLower(coalesce(_b.name, '')) CONTAINS toLower($q) })"
        )
    return clause


def list_extraction_queue(status: str | None, limit: int, sort: str = "recent",
                          score_min: float | None = None,
                          score_max: float | None = None,
                          notice_type: str | None = None,
                          date_from: str | None = None,
                          date_to: str | None = None,
                          q: str | None = None) -> list[dict]:
    clause = _extraction_filter_clause(status, score_min, score_max,
                                       notice_type, date_from, date_to, q)
    # "recent" (default): latest batch first (then newest extraction within it) so
    # a just-run batch groups at the top; docs missing extraction data (extracted
    # before it was tracked) fall to the bottom. "name": alphabetical by filename.
    order = (
        "d.filename"
        if sort == "name"
        else ("d.extraction_at IS NULL, coalesce(d.extraction_batch,-1) DESC, "
              "d.extraction_at DESC, d.filename")
    )
    return run_read_query(
        f"""
        MATCH (d:Document)
        WHERE d.extraction_json IS NOT NULL {clause}
        RETURN d.filename AS filename,
               coalesce(d.extraction_review_status,'pending') AS status,
               d.extraction_score AS score,
               toString(d.extraction_at) AS extraction_at,
               toString(d.markdown_reextracted_at) AS markdown_reextracted_at,
               toString(d.markdown_loaded_at) AS markdown_loaded_at,
               d.extraction_batch AS extraction_batch,
               d.expected_lot_count AS expected_lot_count,
               d.extraction_json AS extraction_json
        ORDER BY {order}
        LIMIT $limit
        """,
        {"status": status, "limit": limit,
         "score_min": score_min, "score_max": score_max,
         "date_from": date_from, "date_to": date_to, "q": q},
        max_rows=5000,
    )


def extraction_stats(score_min: float | None = None,
                     score_max: float | None = None,
                     notice_type: str | None = None,
                     date_from: str | None = None,
                     date_to: str | None = None,
                     q: str | None = None) -> dict:
    """Header pill counts for the extraction stage, under the reviewer's
    current filters (status is deliberately NOT applied — the pills ARE the
    status breakdown)."""
    clause = _extraction_filter_clause(None, score_min, score_max,
                                       notice_type, date_from, date_to, q)
    rows = run_read_query(
        f"""
        MATCH (d:Document)
        WHERE d.extraction_json IS NOT NULL {clause}
        WITH coalesce(d.extraction_review_status,'pending') AS st
        RETURN count(*) AS total,
               sum(CASE WHEN st = 'pending'  THEN 1 ELSE 0 END) AS pending,
               sum(CASE WHEN st = 'verified' THEN 1 ELSE 0 END) AS verified,
               sum(CASE WHEN st = 'edited'   THEN 1 ELSE 0 END) AS edited
        """,
        {"score_min": score_min, "score_max": score_max,
         "date_from": date_from, "date_to": date_to, "q": q},
        max_rows=1, timeout=30.0,
    )
    r = rows[0] if rows else {}
    return {
        "total":    int(r.get("total") or 0),
        "pending":  int(r.get("pending") or 0),
        "verified": int(r.get("verified") or 0),
        "edited":   int(r.get("edited") or 0),
    }


def save_field_correction(filename: str, field_id: str, value: str,
                          by_email: str, notes: str | None) -> bool:
    cur = run_read_query(
        "MATCH (d:Document {filename:$fn}) WHERE d.extraction_json IS NOT NULL "
        "RETURN coalesce(d.extraction_corrections_json,'{}') AS c LIMIT 1",
        {"fn": filename})
    if not cur:
        return False
    try:
        corr = json.loads(cur[0]["c"] or "{}")
    except json.JSONDecodeError:
        corr = {}
    corr[field_id] = {"value": value, "by": by_email, "at": _now(), "notes": notes}
    rows = run_query(
        """
        MATCH (d:Document {filename:$fn})
        SET d.extraction_corrections_json = $c,
            d.extraction_review_status    = 'edited'
        RETURN d.filename AS filename
        """,
        {"fn": filename, "c": json.dumps(corr, ensure_ascii=False)})
    return bool(rows)


def verify_extraction(filename: str, by_email: str, notes: str | None) -> bool:
    rows = run_query(
        """
        MATCH (d:Document {filename:$fn})
        SET d.extraction_review_status = 'verified',
            d.extraction_verified_by   = $by,
            d.extraction_verified_at   = datetime()
        RETURN d.filename AS filename
        """,
        {"fn": filename, "by": by_email, "notes": notes})
    return bool(rows)


def unverify_extraction(filename: str) -> bool:
    rows = run_query(
        """
        MATCH (d:Document {filename:$fn})
        SET d.extraction_review_status = 'edited'
        REMOVE d.extraction_verified_by, d.extraction_verified_at
        RETURN d.filename AS filename
        """,
        {"fn": filename})
    return bool(rows)


# ── shaping ──────────────────────────────────────────────────────────────────
def count_extracted_lots(ents: list[dict]) -> int | None:
    """Distinct lots in one extraction, from entity ``lot_index`` attributes.

    Multi-notice extractions stamp every entity with its lot's index
    (pipeline/langextract_examples.py); single-notice extractions usually
    carry none, so any entities without a single lot_index count as 1 lot.
    Returns None for an empty extraction — no entities is "no claim", not
    "zero lots".
    """
    if not ents:
        return None
    idxs = set()
    for e in ents:
        attrs = e.get("attrs")
        if isinstance(attrs, dict):
            li = attrs.get("lot_index")
            if li not in (None, ""):
                idxs.add(str(li))
    return len(idxs) if idxs else 1


def _build_fields(extraction_json: str, corrections_json: str) -> list[ExtractionField]:
    try:
        ents = json.loads(extraction_json or "[]")
    except json.JSONDecodeError:
        ents = []
    try:
        corr = json.loads(corrections_json or "{}")
    except json.JSONDecodeError:
        corr = {}
    out: list[ExtractionField] = []
    for i, e in enumerate(ents):
        fid = e.get("id") or str(i)
        c = corr.get(fid) or {}
        attrs = e.get("attrs") or {}
        out.append(ExtractionField(
            id=fid, cls=e.get("cls", ""), text=e.get("text", ""),
            start=e.get("start"), end=e.get("end"),
            grounded=e.get("start") is not None,
            lot_index=attrs.get("lot_index"),
            attrs={k: v for k, v in attrs.items() if k != "lot_index"},
            corrected_value=c.get("value"), corrected_by=c.get("by"),
            corrected_at=c.get("at"),
        ))
    return out


# ── single-document LangExtract rerun ────────────────────────────────────────
# In-process job registry: one rerun per document at a time. Entries live only
# for the server process — a restart forgets a finished error, which is fine;
# the graph's extraction_at is the durable record of success.
_RERUNS: dict[str, dict] = {}
_RERUNS_LOCK = threading.Lock()


def _rerun_state(filename: str) -> tuple[bool, str | None]:
    with _RERUNS_LOCK:
        st = _RERUNS.get(filename)
        if not st:
            return False, None
        return st["status"] == "running", st.get("error")


def _rerun_worker(filename: str) -> None:
    """Run the canonical single-document LangExtract path (same code the batch
    scripts use: per-notice-type model routing, validators score, load_extractions
    write shape). Any failure — including langextract not being installed on
    this server — lands in the registry for the UI to surface."""
    try:
        rows = run_read_query(
            "MATCH (d:Document {filename: $fn}) "
            "RETURN d.filename AS filename, d.markdown AS md, "
            "       d.notice_type AS notice_type, "
            "       d.expected_lot_count AS expected_lot_count",
            {"fn": filename})
        if not rows or not (rows[0].get("md") or "").strip():
            raise RuntimeError("document has no markdown to extract from")
        from pipeline.load_extractions import _next_batch
        from scripts.reset_langextract_and_extract import _extract_one
        _extract_one(rows[0], _next_batch(), route=True)
        with _RERUNS_LOCK:
            _RERUNS.pop(filename, None)
    except Exception as e:  # surfaced via rerun_error, never crashes the app
        with _RERUNS_LOCK:
            _RERUNS[filename] = {"status": "error", "error": f"{type(e).__name__}: {e}"}


# ── endpoints ────────────────────────────────────────────────────────────────
def _no_all(v: str | None) -> str | None:
    """The shared filter bar sends 'all' to mean "no filter"; the query layer
    expects None. Blank strings collapse the same way."""
    return None if v in (None, "", "all") else v


class ExtractionStats(BaseModel):
    total: int
    pending: int
    verified: int
    edited: int


# NOTE: declared before the `/{filename:path}` catch-all below, or that route
# swallows it and /stats resolves as a filename.
@router.get("/stats", response_model=ExtractionStats)
def extraction_stats_endpoint(
    score_min: float | None = Query(default=None, ge=0.0, le=100.0),
    score_max: float | None = Query(default=None, ge=0.0, le=100.0),
    notice_type: str | None = Query(default=None),
    date_from: str | None = Query(default=None, max_length=20),
    date_to: str | None = Query(default=None, max_length=20),
    q: str | None = Query(default=None, max_length=200),
    _admin: UserOut = Depends(get_current_admin),
) -> ExtractionStats:
    return ExtractionStats(**extraction_stats(
        score_min=score_min, score_max=score_max,
        notice_type=_no_all(notice_type),
        date_from=date_from, date_to=date_to, q=q,
    ))


@router.get("/queue", response_model=ExtractionQueueOut)
def extraction_queue(
    status: str | None = Query(default=None),
    limit: int = Query(default=200, le=2000),
    sort: str = Query(default="recent", pattern="^(recent|name)$"),
    score_min: float | None = Query(default=None, ge=0.0, le=100.0),
    score_max: float | None = Query(default=None, ge=0.0, le=100.0),
    notice_type: str | None = Query(default=None),
    date_from: str | None = Query(default=None, max_length=20),
    date_to: str | None = Query(default=None, max_length=20),
    q: str | None = Query(default=None, max_length=200),
    _admin: UserOut = Depends(get_current_admin),
) -> ExtractionQueueOut:
    rows = list_extraction_queue(_no_all(status), limit, sort,
                                 score_min=score_min, score_max=score_max,
                                 notice_type=_no_all(notice_type),
                                 date_from=date_from, date_to=date_to, q=q)
    out = []
    for r in rows:
        try:
            ents = json.loads(r["extraction_json"] or "[]")
        except json.JSONDecodeError:
            ents = []
        b = r.get("extraction_batch")
        s = r.get("score")
        elc = r.get("expected_lot_count")
        expected = int(elc) if elc is not None else None
        extracted = count_extracted_lots(ents)
        out.append(ExtractionQueueRow(
            filename=r["filename"], status=r["status"], n_fields=len(ents),
            n_ungrounded=sum(1 for e in ents if e.get("start") is None),
            score=int(s) if s is not None else None,
            extraction_at=r.get("extraction_at"),
            extraction_batch=int(b) if b is not None else None,
            stale=extraction_stale(r.get("markdown_reextracted_at"),
                                   r.get("markdown_loaded_at"),
                                   r.get("extraction_at")),
            expected_lot_count=expected,
            extracted_lot_count=extracted,
            lot_count_mismatch=(expected is not None
                                and extracted is not None
                                and expected != extracted)))
    return ExtractionQueueOut(rows=out, total=len(out))


@router.get("/{filename:path}", response_model=ExtractionReviewOut)
def extraction_detail(
    filename: str,
    _admin: UserOut = Depends(get_current_admin),
) -> ExtractionReviewOut:
    row = get_extraction(filename)
    if row is None:
        raise HTTPException(status_code=404, detail="extraction not found")
    running, error = _rerun_state(filename)
    return ExtractionReviewOut(
        filename=row["filename"], markdown=row.get("markdown"),
        status=row.get("status", "pending"), score=row.get("score"),
        verified_by=row.get("verified_by"), verified_at=row.get("verified_at"),
        public_url=row.get("public_url"), doc_type=row.get("doc_type"),
        content_type=row.get("content_type"),
        stale=extraction_stale(row.get("markdown_reextracted_at"),
                               row.get("markdown_loaded_at"),
                               row.get("extraction_at")),
        rerun_running=running, rerun_error=error,
        fields=_build_fields(row["extraction_json"], row["corrections_json"]),
    )


@router.post("/{filename:path}/field", response_model=ExtractionReviewOut)
def extraction_edit_field(
    filename: str,
    body: FieldEditBody,
    admin: UserOut = Depends(get_current_admin),
) -> ExtractionReviewOut:
    if not save_field_correction(filename, body.field_id, body.value,
                                 admin.email, body.notes):
        raise HTTPException(status_code=404, detail="extraction not found")
    return extraction_detail(filename, admin)


@router.post("/{filename:path}/rerun", response_model=ExtractionReviewOut)
def extraction_rerun(
    filename: str,
    admin: UserOut = Depends(get_current_admin),
) -> ExtractionReviewOut:
    """Re-run LangExtract for one document — the reviewer's follow-through
    after fixing its markdown (re-ingest / block re-OCR). Kicks off a
    background worker and returns immediately with rerun_running=true; the UI
    polls the detail endpoint until it clears. One rerun per document at a
    time; a second click while running is a 409."""
    if get_extraction(filename) is None:
        raise HTTPException(status_code=404, detail="extraction not found")
    with _RERUNS_LOCK:
        st = _RERUNS.get(filename)
        if st and st["status"] == "running":
            raise HTTPException(status_code=409, detail="rerun already running")
        _RERUNS[filename] = {"status": "running"}
    threading.Thread(target=_rerun_worker, args=(filename,),
                     daemon=True, name=f"rerun-{filename[:40]}").start()
    return extraction_detail(filename, admin)


@router.post("/{filename:path}/verify", response_model=ExtractionReviewOut)
def extraction_verify(
    filename: str,
    body: ExtractionVerifyBody,
    admin: UserOut = Depends(get_current_admin),
) -> ExtractionReviewOut:
    if not verify_extraction(filename, admin.email, body.notes):
        raise HTTPException(status_code=404, detail="extraction not found")
    return extraction_detail(filename, admin)


@router.post("/{filename:path}/unverify", response_model=ExtractionReviewOut)
def extraction_unverify(
    filename: str,
    admin: UserOut = Depends(get_current_admin),
) -> ExtractionReviewOut:
    if not unverify_extraction(filename):
        raise HTTPException(status_code=404, detail="extraction not found")
    return extraction_detail(filename, admin)
