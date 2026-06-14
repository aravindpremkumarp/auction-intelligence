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

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response

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
    dossier = await repo.get_dossier(user.id, dossier_id)
    if dossier is None:
        raise HTTPException(status_code=404, detail="dossier not found")
    present = tax.present_doc_type_ids(
        doc.get("doc_type") for doc in dossier.get("documents", [])
    )
    dossier["checklist"] = build_checklist(present)
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
