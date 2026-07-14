"""
pipeline/storage.py
-------------------
Cloudflare R2 (S3-compatible) storage helpers for auction sales-notice
downloads. The bucket is public; the web UI links directly to
f"{R2_PUBLIC_BASE_URL}/{storage_key}".
"""
from __future__ import annotations

import mimetypes
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

R2_ACCOUNT_ID        = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID     = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET            = os.getenv("R2_BUCKET", "")
R2_PUBLIC_BASE_URL   = os.getenv("R2_PUBLIC_BASE_URL", "").rstrip("/")

# Separate PRIVATE bucket for confidential user dossier documents (title deeds,
# EC, legal-heir certs). Never made public: reads go through short-TTL presigned
# URLs minted only after an ownership check. See api/dossier.
R2_PRIVATE_BUCKET    = os.getenv("R2_PRIVATE_BUCKET", "auction-dossiers")

_KEY_PREFIX = "notices"
_DOSSIER_KEY_PREFIX = "dossiers"
_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


class R2ConfigError(RuntimeError):
    """Raised when an R2 operation is attempted without full credentials."""


def _require_config() -> None:
    missing = [
        name for name, val in [
            ("R2_ACCOUNT_ID",        R2_ACCOUNT_ID),
            ("R2_ACCESS_KEY_ID",     R2_ACCESS_KEY_ID),
            ("R2_SECRET_ACCESS_KEY", R2_SECRET_ACCESS_KEY),
            ("R2_BUCKET",            R2_BUCKET),
            ("R2_PUBLIC_BASE_URL",   R2_PUBLIC_BASE_URL),
        ] if not val
    ]
    if missing:
        raise R2ConfigError(f"Missing R2 config: {', '.join(missing)}")


@lru_cache(maxsize=1)
def r2_client():
    """Cached boto3 S3 client pointed at Cloudflare R2."""
    _require_config()
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def _safe_segment(value: str) -> str:
    cleaned = _SAFE_SEGMENT_RE.sub("_", value.strip()).strip("._-")
    return cleaned or "unnamed"


def object_key(auction_id: str, filename: str) -> str:
    """Deterministic R2 object key for an auction's download.

    Shape: ``notices/{auction_id}/{filename}`` with path-unsafe characters
    replaced so the same inputs always produce the same key.
    """
    return f"{_KEY_PREFIX}/{_safe_segment(auction_id)}/{_safe_segment(filename)}"


def guess_content_type(filename: str) -> str:
    """Best-effort MIME type for serving from R2 and for the UI viewer branch."""
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return "application/pdf"
    if ext in (".jpg", ".jpeg", ".jfif"):
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def doc_type_from_content_type(content_type: str) -> str:
    """Broad bucket used by the UI to pick a viewer (``pdf`` | ``image`` | ``other``)."""
    if content_type == "application/pdf":
        return "pdf"
    if content_type.startswith("image/"):
        return "image"
    return "other"


def public_url_for(key: str) -> str:
    """Full browser-accessible URL for a given R2 object key."""
    _require_config()
    return f"{R2_PUBLIC_BASE_URL}/{key}"


def exists(key: str) -> bool:
    """Return True if an object with this key already exists in R2."""
    from botocore.exceptions import ClientError

    client = r2_client()
    try:
        client.head_object(Bucket=R2_BUCKET, Key=key)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def upload_file(local_path: Path | str, key: str, content_type: Optional[str] = None) -> str:
    """Upload ``local_path`` to R2 under ``key``. Returns the public URL.

    Re-uploads are idempotent — callers that want to skip existing objects
    should call :func:`exists` first.
    """
    path = Path(local_path)
    if not path.exists():
        raise FileNotFoundError(path)
    ct = content_type or guess_content_type(path.name)
    client = r2_client()
    with path.open("rb") as fh:
        client.put_object(
            Bucket=R2_BUCKET,
            Key=key,
            Body=fh,
            ContentType=ct,
        )
    return public_url_for(key)


def upload_bytes(key: str, body: bytes, content_type: Optional[str] = None) -> str:
    """Upload raw ``body`` bytes to the PUBLIC bucket under ``key``.

    Returns the public URL. Mirrors :func:`upload_file` for callers that hold
    bytes in memory (e.g. members extracted from a MinerU result zip) rather
    than a file on disk. Idempotent — re-uploads overwrite the same key.
    """
    _require_config()
    client = r2_client()
    client.put_object(
        Bucket=R2_BUCKET,
        Key=key,
        Body=body,
        ContentType=content_type or "application/octet-stream",
    )
    return public_url_for(key)


# ── MinerU raw-artifact archival keys ─────────────────────────────────────────
#
# Going-forward OCR runs archive MinerU's complete result zip (and the image /
# table crops extracted from it) to the public bucket so nothing MinerU emits is
# lost. Keys are namespaced under ``mineru/`` and derived from the Document's
# safe cache name so the same notice always lands on the same key.

_MINERU_KEY_PREFIX = "mineru"


def mineru_zip_key(safe_name: str) -> str:
    """R2 key for a notice's complete MinerU result zip."""
    return f"{_MINERU_KEY_PREFIX}/raw_zips/{_safe_segment(safe_name)}.zip"


def mineru_image_key(safe_name: str, image_name: str) -> str:
    """R2 key for one image/table crop extracted from a MinerU result zip.

    ``image_name`` is the crop's basename (MinerU uses globally-unique
    content-hash filenames, so the basename alone is a safe, stable key).
    """
    return f"{_MINERU_KEY_PREFIX}/images/{_safe_segment(safe_name)}/{_safe_segment(image_name)}"


# ── Private dossier storage ───────────────────────────────────────────────────
#
# The public path above shares one bucket served openly from R2_PUBLIC_BASE_URL.
# Dossier documents are confidential, so they live in a *separate* bucket with
# no public base URL: callers never get a permanent link, only a short-lived
# presigned GET minted after the per-dossier ownership check in the API layer.

def _require_private_config() -> None:
    missing = [
        name for name, val in [
            ("R2_ACCOUNT_ID",        R2_ACCOUNT_ID),
            ("R2_ACCESS_KEY_ID",     R2_ACCESS_KEY_ID),
            ("R2_SECRET_ACCESS_KEY", R2_SECRET_ACCESS_KEY),
            ("R2_PRIVATE_BUCKET",    R2_PRIVATE_BUCKET),
        ] if not val
    ]
    if missing:
        raise R2ConfigError(f"Missing private R2 config: {', '.join(missing)}")


def dossier_object_key(supabase_id: str, dossier_id: str, doc_id: str, filename: str) -> str:
    """Deterministic private key for a dossier document.

    Shape: ``dossiers/{supabase_id}/{dossier_id}/{doc_id}__{filename}`` with
    path-unsafe characters replaced. Namespacing by user + dossier keeps a
    prefix-delete (on dossier deletion) cheap and unambiguous.
    """
    return (
        f"{_DOSSIER_KEY_PREFIX}/{_safe_segment(supabase_id)}/"
        f"{_safe_segment(dossier_id)}/{_safe_segment(doc_id)}__{_safe_segment(filename)}"
    )


def upload_bytes_private(key: str, body: bytes, content_type: Optional[str] = None) -> str:
    """Upload raw bytes to the private dossier bucket. Returns the object key
    (there is no public URL — reads go through :func:`presigned_get_url`)."""
    _require_private_config()
    client = r2_client()
    client.put_object(
        Bucket=R2_PRIVATE_BUCKET,
        Key=key,
        Body=body,
        ContentType=content_type or "application/octet-stream",
    )
    return key


def upload_file_private(local_path: Path | str, key: str, content_type: Optional[str] = None) -> str:
    """Upload ``local_path`` to the PRIVATE bucket under ``key``. Returns the
    object key (no public URL — reads go through :func:`presigned_get_url`).
    Mirrors :func:`upload_file` for private objects streamed from disk
    (channel-research media can be tens of MB, so no bytes-in-memory)."""
    path = Path(local_path)
    if not path.exists():
        raise FileNotFoundError(path)
    _require_private_config()
    ct = content_type or guess_content_type(path.name)
    client = r2_client()
    with path.open("rb") as fh:
        client.put_object(
            Bucket=R2_PRIVATE_BUCKET,
            Key=key,
            Body=fh,
            ContentType=ct,
        )
    return key


def presigned_get_url(key: str, expires_in: int = 300) -> str:
    """Mint a short-TTL presigned GET URL for a private object. Callers MUST
    have already verified the requester owns the dossier this key belongs to."""
    _require_private_config()
    client = r2_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_PRIVATE_BUCKET, "Key": key},
        ExpiresIn=expires_in,
    )


# ── Marketing channel-research storage ────────────────────────────────────────
#
# marketing/research/ pulls third-party social content (Instagram/X) for the
# marketing team's content research. Scraped third-party media must never be
# served publicly from our domain, so runs land in the PRIVATE bucket and are
# shared via short-TTL presigned URLs.

_RESEARCH_KEY_PREFIX = "marketing-research"


def research_object_key(platform: str, channel: str, run_date: str, filename: str) -> str:
    """Deterministic private key for one file of a channel-research run.

    Shape: ``marketing-research/{platform}/{channel}/{run_date}/{filename}``
    with path-unsafe characters replaced. ``filename`` may carry a single
    ``media/`` subdirectory prefix (kept as a real key segment so a run's
    media stays prefix-listable alongside its posts.csv/posts.json).
    """
    parts = filename.split("/", 1)
    safe_name = "/".join(_safe_segment(p) for p in parts)
    return (
        f"{_RESEARCH_KEY_PREFIX}/{_safe_segment(platform)}/"
        f"{_safe_segment(channel)}/{_safe_segment(run_date)}/{safe_name}"
    )


def delete_private_objects(keys: list[str]) -> None:
    """Delete the given private objects (cascade on dossier deletion).

    Best-effort and batched (S3 ``delete_objects`` caps at 1000 keys/call).
    Empty/blank keys are skipped. Raising is left to the caller's discretion.
    """
    _require_private_config()
    clean = [k for k in keys if k]
    if not clean:
        return
    client = r2_client()
    for i in range(0, len(clean), 1000):
        batch = clean[i:i + 1000]
        client.delete_objects(
            Bucket=R2_PRIVATE_BUCKET,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
