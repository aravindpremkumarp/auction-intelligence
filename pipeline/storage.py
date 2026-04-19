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

_KEY_PREFIX = "notices"
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
    if ext in (".jpg", ".jpeg"):
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
