"""Shared MinerU helpers.

MinerU (https://mineru.net) converts notice PDFs/images to layout-aware
markdown. The pipeline caches that markdown under
``pipeline/cache/mineru_markdown/<safe_cache_name(file_path)>.md`` so it can
be reused across stages (OCR extraction, description generation).

This module exposes the helpers that more than one script needs. The full
MinerU API client still lives in ``scripts/ocr_with_mineru.py``.
"""
from __future__ import annotations

from pathlib import Path

from pipeline.config import DOWNLOADS_DIR, PIPELINE_DIR


MINERU_MARKDOWN_DIR = PIPELINE_DIR / "cache" / "mineru_markdown"

MINERU_SUPPORTED_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".jfif"}
MINERU_EXT_REMAP = {".jfif": ".jpg"}


def safe_cache_name(path: str) -> str:
    """Normalize a path/key into a safe single-segment filename."""
    return path.replace("/", "_").replace("\\", "_").replace(":", "_")


def find_disk_path(filename: str, downloads_dir: Path = DOWNLOADS_DIR) -> Path | None:
    """Resolve a Document filename to a concrete file on disk.

    Tries ``downloads/tn_properties/`` first (current scraper layout) and
    falls back to ``downloads/``. Returns ``None`` if neither exists.
    """
    for base in (downloads_dir / "tn_properties", downloads_dir):
        p = base / filename
        if p.exists():
            return p
    return None


def cached_markdown_for_file_path(file_path: str) -> str | None:
    """Read markdown for a Document.file_path value if it has been cached."""
    p = MINERU_MARKDOWN_DIR / f"{safe_cache_name(file_path)}.md"
    if p.exists():
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def cached_markdown_for_filename(filename: str) -> str | None:
    """Look up cached MinerU markdown when only the filename is known.

    Document.file_path historically carried mixed values (bare filename,
    relative ``tn_properties/...``, absolute paths) so the cache key cannot
    be derived from the filename alone. We try the common variants first,
    then fall back to scanning the cache directory for an entry whose
    safe-name ends in the safe form of the filename.
    """
    safe_filename = safe_cache_name(filename)
    candidates = [
        filename,
        f"tn_properties/{filename}",
        f"downloads/tn_properties/{filename}",
        f"downloads/{filename}",
    ]
    for cand in candidates:
        md = cached_markdown_for_file_path(cand)
        if md is not None:
            return md

    if not MINERU_MARKDOWN_DIR.exists():
        return None
    suffix = f"_{safe_filename}.md"
    matches = [p for p in MINERU_MARKDOWN_DIR.iterdir()
               if p.name == f"{safe_filename}.md" or p.name.endswith(suffix)]
    if len(matches) == 1:
        try:
            return matches[0].read_text(encoding="utf-8")
        except OSError:
            return None
    return None
