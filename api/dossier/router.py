"""
api/dossier/router.py
---------------------
`/dossiers` endpoints — a private, per-property document locker.

All routes require a valid Supabase access token and are scoped to the caller's
``supabase_id`` through the ownership edge in ``repository``. Server-minted
UUIDs are used for every id (never trust a client-supplied id).

This is the foundation slice: create / list / read / delete a dossier and see
its have/missing checklist + readiness score. Upload→OCR→classify ingest and
dossier Q&A land in follow-up slices on top of this data model.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile

from api.auth.dependencies import get_current_user
from api.auth.schemas import UserOut
from api.dossier import repository as repo
from api.dossier import taxonomy as tax
from api.dossier.checklist import build_checklist, readiness_score
from api.dossier.schemas import DossierCreateIn

logger = logging.getLogger(__name__)

router = APIRouter()


def _new_id() -> str:
    return uuid.uuid4().hex


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _dossier_with_checklist(user_id: str, dossier_id: str) -> dict | None:
    """Fetch an owned dossier and attach its have/missing checklist, or None."""
    dossier = await repo.get_dossier(user_id, dossier_id)
    if dossier is None:
        return None
    present = tax.present_doc_type_ids(
        doc.get("doc_type") for doc in dossier.get("documents", [])
    )
    dossier["checklist"] = build_checklist(present)
    return dossier


@router.post("/dossiers", status_code=201)
async def create_dossier(
    body: DossierCreateIn,
    user: UserOut = Depends(get_current_user),
) -> dict:
    """Create a dossier for an on-graph auction or an off-graph property."""
    dossier_id = _new_id()
    if body.auction_id:
        title = body.title or f"Dossier for {body.auction_id}"
        dossier = await repo.create_dossier_for_auction(
            user.id, dossier_id, title, body.auction_id
        )
        if dossier is None:
            raise HTTPException(status_code=404, detail="auction not found")
    else:
        up = body.user_property  # guaranteed present by schema validation
        title = body.title or f"Dossier for {up.label}"
        dossier = await repo.create_dossier_for_user_property(
            user.id, dossier_id, title, _new_id(),
            label=up.label, survey_no=up.survey_no,
            sub_registrar=up.sub_registrar, address=up.address,
        )
    # A brand-new dossier has no documents — return the empty checklist so the
    # client can render the readiness UI immediately.
    dossier["checklist"] = build_checklist(set())
    return dossier


@router.get("/dossiers")
async def list_dossiers(user: UserOut = Depends(get_current_user)) -> dict:
    """List the user's dossiers with a readiness score on each (summary view)."""
    rows = await repo.list_dossiers(user.id)
    for d in rows:
        present = tax.present_doc_type_ids(d.pop("doc_types", []))
        d["readiness"] = readiness_score(present)
    return {"dossiers": rows}


@router.get("/dossiers/{dossier_id}")
async def get_dossier(
    dossier_id: str,
    user: UserOut = Depends(get_current_user),
) -> dict:
    """Full dossier view: property, documents, and the have/missing checklist."""
    dossier = await _dossier_with_checklist(user.id, dossier_id)
    if dossier is None:
        raise HTTPException(status_code=404, detail="dossier not found")
    return dossier


@router.delete("/dossiers/{dossier_id}", status_code=204)
async def delete_dossier(
    dossier_id: str,
    user: UserOut = Depends(get_current_user),
) -> Response:
    """Delete a dossier and cascade: R2 objects, document nodes, off-graph
    property node, and the dossier itself."""
    # Resolve R2 keys under the ownership gate *before* any destructive work;
    # None means not found/owned -> 404 (no deletion attempted).
    keys = await repo.get_dossier_r2_keys(user.id, dossier_id)
    if keys is None:
        raise HTTPException(status_code=404, detail="dossier not found")

    # Best-effort object deletion — a storage hiccup must not strand the user
    # with an undeletable dossier; any orphaned objects can be swept later.
    if keys:
        try:
            import asyncio

            from pipeline import storage

            await asyncio.to_thread(storage.delete_private_objects, keys)
        except Exception:  # noqa: BLE001 - log and proceed with graph delete
            logger.exception("R2 cleanup failed for dossier %s", dossier_id)

    await repo.delete_dossier(user.id, dossier_id)
    return Response(status_code=204)


# ── Document ingest ───────────────────────────────────────────────────────────

@router.post("/dossiers/{dossier_id}/documents", status_code=201)
async def upload_document(
    dossier_id: str,
    file: UploadFile = File(...),
    consent: bool = Form(False),
    user: UserOut = Depends(get_current_user),
) -> dict:
    """Upload one document, then synchronously OCR + classify it (sync-with-caps).

    ``consent`` must be true: the user explicitly consents at upload time to the
    file being sent to the OCR provider (consent-and-proceed). The full dossier
    (with refreshed checklist) is returned so the client can update in one round
    trip; ``uploaded_document_id`` points at the new document.

    If OCR/classification fails the document is still recorded with
    ``status="failed"`` (the file is kept) so the user can retry or delete it.
    """
    from pipeline import dossier_ingest, storage

    filename = file.filename or "upload"
    body = await file.read()

    # 1) Caps first (cheap rejects before any storage/graph work).
    try:
        content_type = dossier_ingest.validate_upload(body, filename, file.content_type)
    except dossier_ingest.IngestError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))

    # 2) Consent gate.
    if not consent:
        raise HTTPException(
            status_code=400,
            detail="OCR consent is required before a document can be processed",
        )

    # 3) Ownership — don't push bytes to storage for a dossier we don't own.
    if not await repo.owns_dossier(user.id, dossier_id):
        raise HTTPException(status_code=404, detail="dossier not found")

    doc_id = _new_id()
    r2_key = storage.dossier_object_key(user.id, dossier_id, doc_id, filename)

    # 4) Store the original in the private bucket.
    try:
        await asyncio.to_thread(storage.upload_bytes_private, r2_key, body, content_type)
    except Exception:
        logger.exception("private upload failed for dossier %s", dossier_id)
        raise HTTPException(status_code=502, detail="document storage failed")

    created = await repo.add_document(
        user.id, dossier_id, doc_id,
        filename=filename, r2_key=r2_key, content_type=content_type,
        size_bytes=len(body), status="processing", ocr_consent_at=_now_iso(),
    )
    if not created:
        # Dossier disappeared between the ownership check and the write — don't
        # leave an orphaned object behind.
        await asyncio.to_thread(storage.delete_private_objects, [r2_key])
        raise HTTPException(status_code=404, detail="dossier not found")

    # 5) OCR + classify (synchronous). Failures are recorded, not fatal.
    try:
        result = await dossier_ingest.extract_and_classify(body, filename, content_type)
        await repo.set_document_result(
            user.id, dossier_id, doc_id, status="ready",
            category=result["category"], doc_type=result["doc_type"],
            confidence=result["confidence"], reasoning=result["reasoning"],
            ocr_text=result["markdown"], classified_at=_now_iso(),
        )
    except Exception as e:  # noqa: BLE001 - record failure, keep the upload
        logger.exception("ingest failed for document %s", doc_id)
        await repo.set_document_result(
            user.id, dossier_id, doc_id, status="failed",
            category=None, doc_type=None, confidence=None,
            reasoning=str(e)[:300], ocr_text=None, classified_at=None,
        )

    dossier = await _dossier_with_checklist(user.id, dossier_id)
    if dossier is None:  # pragma: no cover - just deleted concurrently
        raise HTTPException(status_code=404, detail="dossier not found")
    dossier["uploaded_document_id"] = doc_id
    return dossier


@router.get("/dossiers/{dossier_id}/documents/{doc_id}/url")
async def get_document_url(
    dossier_id: str,
    doc_id: str,
    user: UserOut = Depends(get_current_user),
) -> dict:
    """Mint a short-TTL presigned GET URL for an owned document."""
    from pipeline import storage

    doc = await repo.get_document(user.id, dossier_id, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    ttl = 300
    url = await asyncio.to_thread(storage.presigned_get_url, doc["r2_key"], ttl)
    return {
        "url": url, "expires_in": ttl,
        "filename": doc.get("filename"), "content_type": doc.get("content_type"),
    }


@router.delete("/dossiers/{dossier_id}/documents/{doc_id}", status_code=204)
async def delete_document(
    dossier_id: str,
    doc_id: str,
    user: UserOut = Depends(get_current_user),
) -> Response:
    """Delete one document and its private object."""
    key = await repo.delete_document(user.id, dossier_id, doc_id)
    if key is None:
        raise HTTPException(status_code=404, detail="document not found")
    if key:
        try:
            from pipeline import storage

            await asyncio.to_thread(storage.delete_private_objects, [key])
        except Exception:  # noqa: BLE001 - log; node already gone
            logger.exception("R2 cleanup failed for document %s", doc_id)
    return Response(status_code=204)
