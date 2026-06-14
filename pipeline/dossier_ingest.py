"""
pipeline/dossier_ingest.py
--------------------------
Synchronous-with-caps ingest for a single uploaded dossier document:

    validate (type / size / page count)  ->  OCR (MinerU)  ->  classify (taxonomy)

Run inside the request (the decision was sync-with-caps: no queue/worker infra).
The caps in ``pipeline/config.py`` keep one request bounded. The API layer
(api/dossier/router.py) handles consent, R2 storage, and persistence; this
module is the pure "turn bytes into text + a doc-type verdict" core.

Private-data note: OCR here uses MinerU's lower-level helpers and parses the
result zip *in memory* — it deliberately does NOT call ``download_and_cache``,
which would persist confidential user text into the shared on-disk MinerU cache.
"""
from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path

from pipeline.classify_document import classify_document_text
from pipeline.config import DOSSIER_MAX_FILE_MB, DOSSIER_MAX_PAGES
from pipeline.obs import get_logger

log = get_logger(__name__)

MAX_FILE_BYTES = DOSSIER_MAX_FILE_MB * 1024 * 1024
MAX_PAGES = DOSSIER_MAX_PAGES

# What a bidder can sensibly upload: scanned deeds/certs as PDF or photos.
ALLOWED_CONTENT_TYPES = {
    "application/pdf": "pdf",
    "image/png": "image",
    "image/jpeg": "image",
    "image/jpg": "image",
    "image/webp": "image",
}
_EXT_CONTENT_TYPE = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jfif": "image/jpeg",
    ".webp": "image/webp",
}


class IngestError(RuntimeError):
    """Carries the HTTP status the router should surface (413/415/400/502)."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def normalize_content_type(content_type: str | None, filename: str) -> str | None:
    """Resolve a usable content type from the header, falling back to the
    filename extension (browsers send ``application/octet-stream`` for some
    scans). Returns ``None`` if neither yields a supported type."""
    if content_type:
        ct = content_type.split(";", 1)[0].strip().lower()
        if ct in ALLOWED_CONTENT_TYPES:
            return ct
    ext = Path(filename or "").suffix.lower()
    return _EXT_CONTENT_TYPE.get(ext)


def _pdf_page_count(body: bytes) -> int:
    """Page count for a PDF (0 if it can't be opened — treated as 'unknown',
    not as a cap breach, so a quirky-but-valid PDF still gets a shot at OCR)."""
    try:
        import fitz  # type: ignore
    except Exception:  # noqa: BLE001 - PyMuPDF optional on some hosts
        return 0
    try:
        with fitz.open(stream=body, filetype="pdf") as doc:
            return doc.page_count
    except Exception:  # noqa: BLE001
        return 0


def validate_upload(body: bytes, filename: str, content_type: str | None) -> str:
    """Enforce the ingest caps. Returns the normalized content type, or raises
    :class:`IngestError` with the right HTTP status."""
    if not body:
        raise IngestError("empty file", status_code=400)
    if len(body) > MAX_FILE_BYTES:
        raise IngestError(
            f"file too large (max {DOSSIER_MAX_FILE_MB} MB)", status_code=413
        )
    ct = normalize_content_type(content_type, filename)
    if ct is None:
        raise IngestError(
            "unsupported file type (allowed: PDF, PNG, JPEG, WEBP)",
            status_code=415,
        )
    if ct == "application/pdf":
        pages = _pdf_page_count(body)
        if pages > MAX_PAGES:
            raise IngestError(
                f"too many pages ({pages}; max {MAX_PAGES})", status_code=413
            )
    return ct


def _ocr_via_mineru(body: bytes, filename: str) -> str:
    """OCR one file with MinerU, returning markdown. Parses the result zip in
    memory (no shared-cache writes). Blocking — call via ``asyncio.to_thread``."""
    from pipeline.mineru_api import (
        download_zip,
        parse_zip_payload,
        poll,
        request_batch,
        upload_files,
    )

    suffix = Path(filename).suffix or ".pdf"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(body)
        tmp_path = Path(f.name)
    # Unique key so a dossier upload never collides with the bulk pipeline's
    # cache namespace; not derived from user content.
    file_path_key = f"dossier_ingest/{uuid.uuid4().hex}{suffix}"
    item = {"filename": Path(file_path_key).name,
            "file_path": file_path_key, "disk_path": tmp_path}
    try:
        batch_id, urls = request_batch([item])
        upload_files([item], urls)
        results = poll(batch_id, timeout_s=300)
        if not results or results[0].get("state") != "done":
            err = results[0].get("err_msg") if results else "no rows"
            raise IngestError(f"OCR failed: {err}", status_code=502)
        zip_url = results[0].get("full_zip_url")
        if not zip_url:
            raise IngestError("OCR returned no result", status_code=502)
        zip_bytes = download_zip(zip_url)
        if not zip_bytes:
            raise IngestError("OCR result download failed", status_code=502)
        md_text, _ = parse_zip_payload(zip_bytes)
        return md_text or ""
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


async def extract_and_classify(body: bytes, filename: str, content_type: str) -> dict:
    """OCR then doc-type-classify one validated document.

    Returns ``{markdown, category, doc_type, confidence, reasoning}``. Raises
    :class:`IngestError` (status 502) if OCR yields no text at all — there's
    nothing to classify and the caller should mark the doc 'failed'.
    """
    markdown = await asyncio.to_thread(_ocr_via_mineru, body, filename)
    if not markdown.strip():
        raise IngestError("OCR produced no text", status_code=502)
    verdict = await classify_document_text(markdown, filename) or {}
    return {
        "markdown": markdown,
        "category": verdict.get("category") or "unknown",
        "doc_type": verdict.get("doc_type") or "unknown",
        "confidence": verdict.get("confidence"),
        "reasoning": verdict.get("reasoning") or "",
    }
