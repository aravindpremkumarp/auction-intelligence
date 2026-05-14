"""
api/review/router.py
--------------------
Admin-only `/review/*` endpoints powering the enrichment review UI.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.auth.dependencies import get_current_admin
from api.auth.schemas import UserOut
from api.review import queries as q


router = APIRouter(prefix="/review", tags=["review"])


class ReviewQueueRow(BaseModel):
    auction_id: str
    title: str | None = None
    borrowers: list[str] = []
    reserve_price: float | None = None
    completeness: float | None = None
    source: str | None = None
    verified: bool = False
    verified_at: str | None = None
    verified_by: str | None = None
    notice_type: str | None = None
    has_pdf: bool = False


class ReviewQueueOut(BaseModel):
    page: int
    size: int
    total: int
    rows: list[ReviewQueueRow]


class ReviewNoticeProperty(BaseModel):
    auction_id: str
    title: str | None = None
    borrowers: list[str] = []
    reserve_price: float | None = None
    completeness: float | None = None
    source: str | None = None
    verified: bool = False
    verified_at: str | None = None
    verified_by: str | None = None


class ReviewNoticeRow(BaseModel):
    filename: str | None = None
    file_path: str | None = None
    public_url: str | None = None
    notice_type: str | None = None
    doc_property_count: int | None = None
    total_count: int = 0
    pending_count: int = 0
    verified_count: int = 0
    edited_count: int = 0
    properties: list[ReviewNoticeProperty] = []


class ReviewNoticeQueueOut(BaseModel):
    page: int
    size: int
    total: int
    rows: list[ReviewNoticeRow]


class ReviewDocument(BaseModel):
    filename: str | None = None
    file_path: str | None = None
    public_url: str | None = None
    storage_key: str | None = None
    notice_type: str | None = None
    markdown: str | None = None


class ReviewPropertyOut(BaseModel):
    auction_id: str
    title: str | None = None
    url: str | None = None
    reserve_price: float | None = None
    auction_start: str | None = None
    city: str | None = None
    area: str | None = None
    borrowers: list[str] = []
    description: str | None = None
    description_scraped: str | None = None
    enriched_description: str | None = None
    website_description: str | None = None
    description_source: str | None = None
    description_extracted_original: str | None = None
    extracted_description: str | None = None
    completeness: float | None = None
    verified: bool = False
    verified_at: str | None = None
    verified_by: str | None = None
    review_notes: str | None = None
    documents: list[ReviewDocument] = []


class VerifyBody(BaseModel):
    notes: str | None = Field(default=None, max_length=2000)


class EditBody(BaseModel):
    description: str = Field(min_length=1, max_length=20000)
    notes: str | None = Field(default=None, max_length=2000)


class ReviewStats(BaseModel):
    total: int
    pending: int
    verified: int
    edited: int


def _row_to_str(row: dict) -> dict:
    """Stringify Neo4j datetime fields so Pydantic can serialize them."""
    out = dict(row)
    for k in ("verified_at",):
        v = out.get(k)
        if v is not None and not isinstance(v, str):
            out[k] = str(v)
    return out


@router.get("/stats", response_model=ReviewStats)
async def review_stats(
    date_from: str | None = Query(default=None, max_length=20),
    date_to: str | None = Query(default=None, max_length=20),
    _admin: UserOut = Depends(get_current_admin),
) -> ReviewStats:
    return ReviewStats(**q.stats(date_from=date_from, date_to=date_to))


@router.get("/queue", response_model=ReviewQueueOut)
async def review_queue(
    status: Literal["pending", "verified", "edited", "all"] = "pending",
    q_search: str | None = Query(default=None, alias="q", max_length=200),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
    date_from: str | None = Query(default=None, max_length=20),
    date_to: str | None = Query(default=None, max_length=20),
    _admin: UserOut = Depends(get_current_admin),
) -> ReviewQueueOut:
    result = q.list_queue(
        status=status, q=q_search, page=page, size=size,
        date_from=date_from, date_to=date_to,
    )
    rows = [ReviewQueueRow(**_row_to_str(r)) for r in result["rows"]]
    return ReviewQueueOut(page=result["page"], size=result["size"], total=result["total"], rows=rows)


@router.get("/notices", response_model=ReviewNoticeQueueOut)
async def review_notices(
    status: Literal["pending", "verified", "edited", "all"] = "pending",
    q_search: str | None = Query(default=None, alias="q", max_length=200),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
    date_from: str | None = Query(default=None, max_length=20),
    date_to: str | None = Query(default=None, max_length=20),
    _admin: UserOut = Depends(get_current_admin),
) -> ReviewNoticeQueueOut:
    result = q.list_notice_queue(
        status=status, q=q_search, page=page, size=size,
        date_from=date_from, date_to=date_to,
    )
    rows = [ReviewNoticeRow(**r) for r in result["rows"]]
    return ReviewNoticeQueueOut(page=result["page"], size=result["size"], total=result["total"], rows=rows)


@router.get("/property/{auction_id}", response_model=ReviewPropertyOut)
async def review_property(
    auction_id: str,
    _admin: UserOut = Depends(get_current_admin),
) -> ReviewPropertyOut:
    row = q.get_property(auction_id)
    if row is None:
        raise HTTPException(status_code=404, detail="property not found")
    return ReviewPropertyOut(**_row_to_str(row))


@router.post("/property/{auction_id}/verify", response_model=ReviewPropertyOut)
async def review_verify(
    auction_id: str,
    body: VerifyBody,
    admin: UserOut = Depends(get_current_admin),
) -> ReviewPropertyOut:
    ok = q.verify(auction_id, by_email=admin.email, notes=body.notes)
    if not ok:
        raise HTTPException(status_code=404, detail="property not found")
    row = q.get_property(auction_id)
    return ReviewPropertyOut(**_row_to_str(row))


@router.post("/property/{auction_id}/edit", response_model=ReviewPropertyOut)
async def review_edit(
    auction_id: str,
    body: EditBody,
    admin: UserOut = Depends(get_current_admin),
) -> ReviewPropertyOut:
    ok = q.edit(auction_id, description=body.description, by_email=admin.email, notes=body.notes)
    if not ok:
        raise HTTPException(status_code=404, detail="property not found")
    row = q.get_property(auction_id)
    return ReviewPropertyOut(**_row_to_str(row))


@router.post("/property/{auction_id}/unverify", response_model=ReviewPropertyOut)
async def review_unverify(
    auction_id: str,
    _admin: UserOut = Depends(get_current_admin),
) -> ReviewPropertyOut:
    ok = q.unverify(auction_id)
    if not ok:
        raise HTTPException(status_code=404, detail="property not found")
    row = q.get_property(auction_id)
    return ReviewPropertyOut(**_row_to_str(row))
