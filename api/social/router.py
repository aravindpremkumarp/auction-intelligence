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

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/social", tags=["social"])

_MEDIA_TYPES = {".png": "image/png", ".json": "application/json", ".md": "text/markdown"}


def _key(date: str, kind: str, stem: str) -> str:
    return f"{date}/{kind}/{stem}"


def _stream_private(key: str, headers: dict[str, str], fallback_type: str) -> Response:
    """Proxy one private R2 object through this admin-gated route.

    Proxied rather than handed over as a presigned URL (the idiom api/dossier
    uses) because a presigned link leaves our auth gate: it works for anyone
    holding it, for its whole TTL. The page fetches with its bearer token and
    renders the result as a blob, so unpublished media is never reachable
    without an admin session.
    """
    from botocore.exceptions import ClientError

    from pipeline import storage

    try:
        obj = storage.get_private_object(key)
    except storage.R2ConfigError as exc:
        raise HTTPException(status_code=503, detail="media storage not configured") from exc
    except ClientError as exc:
        # The manifest records a key the bucket no longer has (lifecycle rule,
        # manual cleanup). A 404 is the honest answer, not a 500.
        raise HTTPException(status_code=404, detail="object missing") from exc

    body = obj["Body"]

    def _iter():
        try:
            for chunk in body.iter_chunks(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            body.close()

    out = dict(headers)
    if obj.get("ContentLength") is not None:
        out["Content-Length"] = str(obj["ContentLength"])
    return StreamingResponse(
        _iter(),
        media_type=obj.get("ContentType") or fallback_type,
        headers=out,
    )


def _fetch_media_blobs(keys: dict[str, str]) -> dict[str, bytes]:
    """Pull a draft's R2-hosted media for the bundle, skipping any that fail.

    An asset that can't be fetched (R2 unconfigured, object deleted) must not
    cost the reviewer the rest of the download, so every failure is swallowed
    here and simply omits that file.
    """
    if not keys:
        return {}
    from pipeline import storage

    out: dict[str, bytes] = {}
    for arcname, key in keys.items():
        try:
            obj = storage.get_private_object(key)
            body = obj["Body"]
            try:
                out[arcname] = body.read()
            finally:
                body.close()
        except Exception:  # noqa: BLE001 - best effort; the zip ships without it
            logger.warning("media %s (%s) unavailable for bundle", arcname, key)
    return out


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
        video_available=bool(artifact.get("video_key")),
        video_bytes=artifact.get("video_bytes"),
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
    """Serve one staged file (card PNG, carousel slide, island JSON, review.md).

    Rendered media lives in R2 and the text-ish files (islands, review.md) stay
    in git, so this checks the manifest's `media_keys` first and falls back to
    the filesystem. The fallback is not vestigial: batches staged before the
    move still have their PNGs committed, and they must keep rendering.

    Every failure mode — bad date, traversal attempt, unserved file type,
    missing file — becomes a flat 404, so probing cannot distinguish "blocked"
    from "absent".
    """
    headers = {"Cache-Control": "private, max-age=60"}
    filename = relpath.rsplit("/", 1)[-1]
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'

    try:
        key = service.media_key_for(date, relpath)
    except ValueError:
        raise HTTPException(status_code=404, detail="asset not found") from None
    if key:
        return _stream_private(key, headers, fallback_type=_MEDIA_TYPES.get(
            Path(filename).suffix.lower(), "application/octet-stream"))

    try:
        path = service.resolve_asset(date, relpath)
    except ValueError:
        raise HTTPException(status_code=404, detail="asset not found") from None
    return FileResponse(
        str(path),
        media_type=_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        headers=headers,
    )


@router.get("/reel/{date}/{stem}")
def get_reel_video(
    date: str,
    stem: str,
    _admin: UserOut = Depends(get_current_admin),
) -> Response:
    """Stream one staged reel's MP4 from the private R2 bucket.

    Kept separate from /asset because the page addresses reels by stem, not by
    path — it never sees where the MP4 lives.
    """
    try:
        batch = service.load_batch(date)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    every = [a for d in batch["drafts"] for a in d["artifacts"]] + batch["orphan_artifacts"]
    match = [a for a in every if a["kind"] == "reel" and a["stem"] == stem]
    if not match or not match[0].get("video_key"):
        raise HTTPException(status_code=404, detail="no rendered reel for this stem")

    return _stream_private(
        match[0]["video_key"],
        {"Cache-Control": "private, max-age=300"},
        fallback_type="video/mp4",
    )


@router.get("/bundle/{date}/{draft_index}")
def get_bundle(
    date: str,
    draft_index: int,
    _admin: UserOut = Depends(get_current_admin),
) -> Response:
    """Zip of one draft's caption, islands, rendered images and reel MP4s.

    Rendered media lives in private R2 rather than on disk, so it's fetched here
    and handed to the bundler. Best-effort: an object that has gone missing is
    left out rather than failing the whole download — the captions and islands
    are still worth having.
    """
    try:
        extras = _fetch_media_blobs(service.draft_media_keys(date, draft_index))
        filename, blob = service.bundle_draft(date, draft_index, extra_files=extras)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=blob,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
