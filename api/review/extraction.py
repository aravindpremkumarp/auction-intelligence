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
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.auth.dependencies import get_current_admin
from api.auth.schemas import UserOut
from api.neo4j_client import run_query, run_read_query

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
    verified_by: str | None = None
    verified_at: str | None = None
    # Source-notice location so the review UI can show the original document
    # next to the markdown (Document props set by scripts/upload_downloads_to_r2).
    public_url: str | None = None
    doc_type: str | None = None  # "image" | "pdf" | "other"
    content_type: str | None = None
    fields: list[ExtractionField] = []


class ExtractionQueueRow(BaseModel):
    filename: str
    status: str
    n_fields: int
    n_ungrounded: int


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
               d.extraction_verified_by                     AS verified_by,
               toString(d.extraction_verified_at)           AS verified_at,
               d.public_url                                 AS public_url,
               d.doc_type                                   AS doc_type,
               d.content_type                               AS content_type
        LIMIT 1
        """,
        {"fn": filename},
    )
    return rows[0] if rows else None


def list_extraction_queue(status: str | None, limit: int) -> list[dict]:
    clause = "AND coalesce(d.extraction_review_status,'pending') = $status" if status else ""
    return run_read_query(
        f"""
        MATCH (d:Document)
        WHERE d.extraction_json IS NOT NULL {clause}
        RETURN d.filename AS filename,
               coalesce(d.extraction_review_status,'pending') AS status,
               d.extraction_json AS extraction_json
        ORDER BY d.filename
        LIMIT $limit
        """,
        {"status": status, "limit": limit},
        max_rows=5000,
    )


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


# ── endpoints ────────────────────────────────────────────────────────────────
@router.get("/queue", response_model=ExtractionQueueOut)
def extraction_queue(
    status: str | None = Query(default=None),
    limit: int = Query(default=200, le=2000),
    _admin: UserOut = Depends(get_current_admin),
) -> ExtractionQueueOut:
    rows = list_extraction_queue(status, limit)
    out = []
    for r in rows:
        try:
            ents = json.loads(r["extraction_json"] or "[]")
        except json.JSONDecodeError:
            ents = []
        out.append(ExtractionQueueRow(
            filename=r["filename"], status=r["status"], n_fields=len(ents),
            n_ungrounded=sum(1 for e in ents if e.get("start") is None)))
    return ExtractionQueueOut(rows=out, total=len(out))


@router.get("/{filename:path}", response_model=ExtractionReviewOut)
def extraction_detail(
    filename: str,
    _admin: UserOut = Depends(get_current_admin),
) -> ExtractionReviewOut:
    row = get_extraction(filename)
    if row is None:
        raise HTTPException(status_code=404, detail="extraction not found")
    return ExtractionReviewOut(
        filename=row["filename"], markdown=row.get("markdown"),
        status=row.get("status", "pending"),
        verified_by=row.get("verified_by"), verified_at=row.get("verified_at"),
        public_url=row.get("public_url"), doc_type=row.get("doc_type"),
        content_type=row.get("content_type"),
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
