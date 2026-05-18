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
from api.review import blocks as block_ops
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


class ClassificationRow(BaseModel):
    filename: str | None = None
    file_path: str | None = None
    public_url: str | None = None
    notice_type: str | None = None
    property_count: int | None = None
    classifier_pred: str | None = None
    classifier_confidence: float | None = None
    classifier_reasoning: str | None = None
    classifier_model: str | None = None
    classified_at: str | None = None
    overridden: bool = False
    verified: bool = False
    verified_at: str | None = None
    verified_by: str | None = None
    review_notes: str | None = None
    extraction_status: str | None = None
    disagreement: bool = False
    sample_titles: list[str] = []
    auction_id_count: int = 0


class ClassificationQueueOut(BaseModel):
    page: int
    size: int
    total: int
    rows: list[ClassificationRow]


class ClassificationStats(BaseModel):
    total: int
    pending: int
    disagreement: int
    verified: int


class ClassifyBody(BaseModel):
    notice_type: Literal["single", "multi"]
    notes: str | None = Field(default=None, max_length=2000)


class ClassifyResult(BaseModel):
    filename: str | None = None
    notice_type: str | None = None
    verified_at: str | None = None
    verified_by: str | None = None
    review_notes: str | None = None
    extraction_status: str | None = None
    invalidated_count: int = 0


class BulkConfirmBody(BaseModel):
    confidence_min: float = Field(ge=0.0, le=1.0)
    notes: str | None = Field(default=None, max_length=2000)
    dry_run: bool = False


class BulkConfirmResult(BaseModel):
    count: int
    dry_run: bool


# ── Markdown-quality review models ──────────────────────────────────────────


class MarkdownRow(BaseModel):
    filename: str | None = None
    file_path: str | None = None
    public_url: str | None = None
    notice_type: str | None = None
    property_count: int | None = None
    markdown_length: int | None = None
    markdown: str | None = None
    score: float | None = None
    quality: Literal["good", "bad"] | None = None
    verified: bool = False
    verified_at: str | None = None
    verified_by: str | None = None
    review_notes: str | None = None


class MarkdownQueueOut(BaseModel):
    page: int
    size: int
    total: int
    rows: list[MarkdownRow]


class MarkdownStats(BaseModel):
    total: int
    pending: int
    good: int
    bad: int
    unscored: int
    auto_confirmable: int


class VerifyMarkdownBody(BaseModel):
    quality: Literal["good", "bad"]
    notes: str | None = Field(default=None, max_length=2000)


class MarkdownBulkConfirmBody(BaseModel):
    score_min: float = Field(ge=0.0, le=100.0)
    notes: str | None = Field(default=None, max_length=2000)
    dry_run: bool = False


# ── Per-block annotator models ──────────────────────────────────────────────


class TableShape(BaseModel):
    format: str = "html"
    rows: int | None = None
    cols: int | None = None
    row_positions: list[float] | None = None
    col_positions: list[float] | None = None


class Block(BaseModel):
    id: str
    page: int = 1
    bbox: list[float]
    label: str
    text: str | None = None
    reading_order: int = 0
    source: Literal["mineru", "human"] = "mineru"
    confidence: float | None = None
    table: TableShape | None = None
    edited_at: str | None = None
    edited_by: str | None = None


class SourceDim(BaseModel):
    page: int
    width: int | None = None
    height: int | None = None


class BlocksDoc(BaseModel):
    filename: str | None = None
    file_path: str | None = None
    public_url: str | None = None
    storage_key: str | None = None
    notice_type: str | None = None
    markdown: str | None = None
    schema_version: int = 1
    source_dims: list[SourceDim] = []
    blocks: list[Block] = []
    blocks_revision: int = 0
    backfill_required: bool = False


class BlockUpdateBody(BaseModel):
    bbox: list[float] | None = None
    label: str | None = None
    text: str | None = None
    reading_order: int | None = None
    table: TableShape | None = None


class BlockCreateBody(BaseModel):
    page: int = 1
    bbox: list[float]
    label: str = "Text"
    text: str | None = ""
    reading_order: int | None = None
    table: TableShape | None = None


class ReorderItem(BaseModel):
    id: str
    reading_order: int


class ReorderBody(BaseModel):
    order: list[ReorderItem]


class ReExtractBody(BaseModel):
    bbox: list[float] | None = None
    label: str | None = None
    page: int | None = None
    row_positions: list[float] | None = None
    col_positions: list[float] | None = None


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


# ── Classification review ───────────────────────────────────────────────────


@router.get("/classifications", response_model=ClassificationQueueOut)
async def review_classifications(
    status: Literal["pending", "disagreement", "verified", "all"] = "pending",
    q_search: str | None = Query(default=None, alias="q", max_length=200),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
    confidence_min: float | None = Query(default=None, ge=0.0, le=1.0),
    agrees_only: bool = Query(default=False),
    _admin: UserOut = Depends(get_current_admin),
) -> ClassificationQueueOut:
    result = q.list_classification_queue(
        status=status, q=q_search, page=page, size=size,
        confidence_min=confidence_min, agrees_only=agrees_only,
    )
    rows = [ClassificationRow(**r) for r in result["rows"]]
    return ClassificationQueueOut(
        page=result["page"], size=result["size"],
        total=result["total"], rows=rows,
    )


@router.post("/classifications/bulk-confirm", response_model=BulkConfirmResult)
async def review_bulk_confirm(
    body: BulkConfirmBody,
    admin: UserOut = Depends(get_current_admin),
) -> BulkConfirmResult:
    result = q.auto_confirm_classifications(
        confidence_min=body.confidence_min,
        by_email=admin.email,
        notes=body.notes,
        dry_run=body.dry_run,
    )
    return BulkConfirmResult(**result)


@router.get("/classifications/stats", response_model=ClassificationStats)
async def review_classification_stats(
    _admin: UserOut = Depends(get_current_admin),
) -> ClassificationStats:
    return ClassificationStats(**q.classification_stats())


@router.post("/notice/{filename}/classify", response_model=ClassifyResult)
async def review_classify(
    filename: str,
    body: ClassifyBody,
    admin: UserOut = Depends(get_current_admin),
) -> ClassifyResult:
    row = q.verify_classification(
        filename=filename, notice_type=body.notice_type,
        by_email=admin.email, notes=body.notes,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="notice not found")
    return ClassifyResult(**row)


# ── Markdown-quality review ─────────────────────────────────────────────────


@router.get("/markdown/stats", response_model=MarkdownStats)
async def review_markdown_stats(
    score_min: float = Query(default=70.0, ge=0.0, le=100.0),
    _admin: UserOut = Depends(get_current_admin),
) -> MarkdownStats:
    return MarkdownStats(**q.markdown_stats(score_min=score_min))


@router.get("/markdown", response_model=MarkdownQueueOut)
async def review_markdown_queue(
    status: Literal["pending", "good", "bad", "unscored", "all"] = "pending",
    q_search: str | None = Query(default=None, alias="q", max_length=200),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
    score_min: float | None = Query(default=None, ge=0.0, le=100.0),
    _admin: UserOut = Depends(get_current_admin),
) -> MarkdownQueueOut:
    result = q.list_markdown_queue(
        status=status, q=q_search, page=page, size=size, score_min=score_min,
    )
    rows = [MarkdownRow(**r) for r in result["rows"]]
    return MarkdownQueueOut(
        page=result["page"], size=result["size"],
        total=result["total"], rows=rows,
    )


@router.post("/markdown/bulk-confirm", response_model=BulkConfirmResult)
async def review_markdown_bulk_confirm(
    body: MarkdownBulkConfirmBody,
    admin: UserOut = Depends(get_current_admin),
) -> BulkConfirmResult:
    result = q.auto_confirm_markdown(
        score_min=body.score_min,
        by_email=admin.email,
        notes=body.notes,
        dry_run=body.dry_run,
    )
    return BulkConfirmResult(**result)


@router.post("/markdown/{filename}/verify", response_model=MarkdownRow)
async def review_markdown_verify(
    filename: str,
    body: VerifyMarkdownBody,
    admin: UserOut = Depends(get_current_admin),
) -> MarkdownRow:
    try:
        row = q.verify_markdown(
            filename=filename, quality=body.quality,
            by_email=admin.email, notes=body.notes,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if row is None:
        raise HTTPException(status_code=404, detail="notice not found")
    return MarkdownRow(**row)


# ── Per-block annotator endpoints ───────────────────────────────────────────


def _ok_block(b: dict) -> Block:
    return Block(**b)


def _ok_doc(doc: dict) -> BlocksDoc:
    blocks = [Block(**b) for b in (doc.get("blocks") or [])]
    return BlocksDoc(
        filename=doc.get("filename"),
        file_path=doc.get("file_path"),
        public_url=doc.get("public_url"),
        storage_key=doc.get("storage_key"),
        notice_type=doc.get("notice_type"),
        markdown=doc.get("markdown"),
        schema_version=int(doc.get("schema_version") or 1),
        source_dims=[SourceDim(**d) for d in (doc.get("source_dims") or [])
                     if isinstance(d, dict) and "page" in d],
        blocks=blocks,
        blocks_revision=int(doc.get("blocks_revision") or 0),
        backfill_required=bool(doc.get("backfill_required")),
    )


def _wrap_block_errors(fn):
    """Map blocks-module exceptions to HTTPException."""
    from functools import wraps

    @wraps(fn)
    async def inner(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except block_ops.BlocksNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
        except block_ops.BlocksConflict as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return inner


@router.get("/notice/{filename}/blocks", response_model=BlocksDoc)
@_wrap_block_errors
async def review_notice_get_blocks(
    filename: str,
    _admin: UserOut = Depends(get_current_admin),
) -> BlocksDoc:
    return _ok_doc(block_ops.get_blocks(filename))


@router.post("/notice/{filename}/blocks", response_model=Block, status_code=201)
@_wrap_block_errors
async def review_notice_create_block(
    filename: str,
    body: BlockCreateBody,
    admin: UserOut = Depends(get_current_admin),
) -> Block:
    blk = block_ops.create_block(
        filename, body.model_dump(exclude_none=True), by_email=admin.email,
    )
    return _ok_block(blk)


@router.put("/notice/{filename}/blocks/{block_id}", response_model=Block)
@_wrap_block_errors
async def review_notice_update_block(
    filename: str,
    block_id: str,
    body: BlockUpdateBody,
    admin: UserOut = Depends(get_current_admin),
) -> Block:
    blk = block_ops.update_block(
        filename, block_id, body.model_dump(exclude_unset=True),
        by_email=admin.email,
    )
    return _ok_block(blk)


@router.delete("/notice/{filename}/blocks/{block_id}")
@_wrap_block_errors
async def review_notice_delete_block(
    filename: str,
    block_id: str,
    _admin: UserOut = Depends(get_current_admin),
) -> dict:
    block_ops.delete_block(filename, block_id)
    return {"ok": True}


@router.post("/notice/{filename}/blocks/reorder", response_model=BlocksDoc)
@_wrap_block_errors
async def review_notice_reorder_blocks(
    filename: str,
    body: ReorderBody,
    admin: UserOut = Depends(get_current_admin),
) -> BlocksDoc:
    order = [item.model_dump() for item in body.order]
    return _ok_doc(block_ops.reorder_blocks(filename, order, by_email=admin.email))


@router.post(
    "/notice/{filename}/blocks/{block_id}/re-extract",
    response_model=Block,
)
@_wrap_block_errors
async def review_notice_reextract_block(
    filename: str,
    block_id: str,
    body: ReExtractBody,
    admin: UserOut = Depends(get_current_admin),
) -> Block:
    blk = await block_ops.re_extract_block(
        filename, block_id, body.model_dump(exclude_none=True),
        by_email=admin.email,
    )
    return _ok_block(blk)


@router.post("/notice/{filename}/reingest", response_model=BlocksDoc)
@_wrap_block_errors
async def review_notice_reingest(
    filename: str,
    admin: UserOut = Depends(get_current_admin),
) -> BlocksDoc:
    return _ok_doc(block_ops.reingest_notice(filename, by_email=admin.email))
