"""
api/social/router.py
--------------------
Admin-only `/social/*` endpoints backing web/social.html.

Two halves joined at read time: the committed batch on disk (api/social/service)
and the review decision in Neo4j (`:SocialContent`). "Pending" is the absence of
a node — mirroring `coalesce(d.extraction_review_status, 'pending')` in
api/review/extraction.py — so setting a status back to pending deletes the node
and there is exactly one representation of "nobody has decided yet".

Assets are served through this router rather than linked straight off the
filesystem: unlike the notice proxy in api/review/router.py (whose bytes are
already public on R2), staged marketing content is unpublished pre-release
material, so it stays behind the admin dependency. The page fetches previews
with its bearer token and renders them as blob: URLs.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse

from api.auth.dependencies import get_current_admin
from api.auth.rate_limit import PUBLIC_READ_LIMIT, limiter
from api.auth.schemas import UserOut
from api.neo4j_client import run_query, run_read_query
from api.social import service
from api.social.schemas import (
    ArtifactOut,
    BatchDetail,
    BatchSummary,
    DraftOut,
    StatusCounts,
    StatusIn,
)

router = APIRouter(prefix="/social", tags=["social"])

_MEDIA_TYPES = {".png": "image/png", ".json": "application/json", ".md": "text/markdown"}


def _key(date: str, kind: str, stem: str) -> str:
    return f"{date}/{kind}/{stem}"


def _statuses_for(date: str) -> dict[str, dict]:
    """Stored decisions for one batch, keyed by `:SocialContent.key`."""
    rows = run_read_query(
        """
        MATCH (s:SocialContent {batch_date: $batch_date})
        RETURN s { .*, updated_at: toString(s.updated_at) } AS s
        """,
        {"batch_date": date},
    )
    return {r["s"]["key"]: r["s"] for r in rows if r.get("s", {}).get("key")}


def _apply_status(artifact: dict, stored: dict[str, dict], date: str) -> ArtifactOut:
    s = stored.get(_key(date, artifact["kind"], artifact["stem"])) or {}
    return ArtifactOut(
        kind=artifact["kind"],
        stem=artifact["stem"],
        template=artifact["template"],
        auction_id=artifact.get("auction_id"),
        headline=artifact.get("headline"),
        island_path=artifact["island_path"],
        png_paths=artifact["png_paths"],
        png_available=artifact["png_available"],
        hook=artifact.get("hook"),
        status=s.get("status") or "pending",
        note=s.get("note"),
        posted_url=s.get("posted_url"),
        updated_at=s.get("updated_at"),
        updated_by_email=s.get("updated_by_email"),
    )


@router.get("/batches", response_model=list[BatchSummary])
@limiter.limit(PUBLIC_READ_LIMIT)
def list_batches(
    request: Request,
    _admin: UserOut = Depends(get_current_admin),
) -> list[BatchSummary]:
    """Every staged batch, newest first, with its review rollup."""
    rollup: dict[str, dict[str, int]] = {}
    for row in run_read_query(
        """
        MATCH (s:SocialContent)
        RETURN s.batch_date AS batch_date, s.status AS status, count(*) AS n
        """,
        {},
    ):
        date = row.get("batch_date")
        status = row.get("status")
        if date and status:
            rollup.setdefault(date, {})[status] = int(row.get("n") or 0)

    out: list[BatchSummary] = []
    for b in service.list_batches():
        counts = rollup.get(b["date"], {})
        out.append(BatchSummary(
            **b,
            statuses=StatusCounts(
                approved=counts.get("approved", 0),
                rejected=counts.get("rejected", 0),
                posted=counts.get("posted", 0),
            ),
        ))
    return out


@router.get("/batch/{date}", response_model=BatchDetail)
@limiter.limit(PUBLIC_READ_LIMIT)
def get_batch(
    date: str,
    request: Request,
    _admin: UserOut = Depends(get_current_admin),
) -> BatchDetail:
    try:
        batch = service.load_batch(date)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    stored = _statuses_for(date)
    drafts = [
        DraftOut(
            **{k: v for k, v in d.items() if k != "artifacts"},
            artifacts=[_apply_status(a, stored, date) for a in d["artifacts"]],
        )
        for d in batch["drafts"]
    ]
    return BatchDetail(
        date=batch["date"],
        generated_at=batch["generated_at"],
        editor_notes=batch["editor_notes"],
        stats=batch["stats"],
        drafts=drafts,
        orphan_artifacts=[_apply_status(a, stored, date) for a in batch["orphan_artifacts"]],
        rejected=batch["rejected"],
    )


@router.patch("/item/{date}/{kind}/{stem}")
def set_item_status(
    date: str,
    kind: str,
    stem: str,
    body: StatusIn,
    admin: UserOut = Depends(get_current_admin),
) -> dict:
    """Record (or clear) the review decision for one artifact.

    The artifact must exist in the batch on disk — otherwise a typo'd stem would
    silently create a status node for something nobody can ever see.
    """
    try:
        batch = service.load_batch(date)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    every = [a for d in batch["drafts"] for a in d["artifacts"]] + batch["orphan_artifacts"]
    match = [a for a in every if a["kind"] == kind and a["stem"] == stem]
    if not match:
        raise HTTPException(status_code=404, detail=f"no {kind} {stem!r} in batch {date}")
    artifact = match[0]
    key = _key(date, kind, stem)

    if body.status == "pending":
        run_query(
            "MATCH (s:SocialContent {key: $key}) DELETE s RETURN count(*) AS deleted",
            {"key": key},
        )
        return {"key": key, "status": "pending"}

    params: dict[str, Any] = {
        "key": key,
        "batch_date": date,
        "kind": kind,
        "stem": stem,
        "auction_id": artifact.get("auction_id"),
        "status": body.status,
        "note": body.note,
        "posted_url": body.posted_url,
        "updated_by": admin.id,
        "updated_by_email": admin.email,
    }
    rows = run_query(
        """
        MERGE (s:SocialContent {key: $key})
        SET s.batch_date = $batch_date, s.kind = $kind, s.stem = $stem,
            s.auction_id = $auction_id, s.status = $status, s.note = $note,
            s.posted_url = $posted_url, s.updated_at = datetime(),
            s.updated_by = $updated_by, s.updated_by_email = $updated_by_email
        RETURN s { .*, updated_at: toString(s.updated_at) } AS s
        """,
        params,
    )
    return rows[0]["s"] if rows else {"key": key, "status": body.status}


@router.get("/asset/{date}/{relpath:path}")
def get_asset(
    date: str,
    relpath: str,
    download: bool = False,
    _admin: UserOut = Depends(get_current_admin),
) -> Response:
    """Serve one staged file (card PNG, island JSON, review.md) from the batch.

    Every failure mode — bad date, traversal attempt, unserved file type,
    missing file — raises ValueError in the service and lands here as a flat
    404, so probing cannot distinguish "blocked" from "absent".
    """
    try:
        path = service.resolve_asset(date, relpath)
    except ValueError:
        raise HTTPException(status_code=404, detail="asset not found") from None
    headers = {"Cache-Control": "private, max-age=60"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    return FileResponse(
        str(path),
        media_type=_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        headers=headers,
    )


@router.get("/bundle/{date}/{draft_index}")
def get_bundle(
    date: str,
    draft_index: int,
    _admin: UserOut = Depends(get_current_admin),
) -> Response:
    """Zip of one draft's caption, islands and rendered images."""
    try:
        filename, blob = service.bundle_draft(date, draft_index)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=blob,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
