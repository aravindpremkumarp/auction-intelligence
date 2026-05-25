"""MinerU API v4 client.

Wraps the four HTTP calls needed to push files through MinerU's batched
"vlm" model:

  1. ``POST /file-urls/batch`` -> ``batch_id`` + signed OSS upload URLs
  2. ``PUT`` each file body to its signed URL
  3. ``GET /extract-results/batch/{batch_id}`` until every row is done
  4. ``GET full_zip_url`` -> unzip ``full.md`` and ``*_content_list.json``

The previous home of these helpers was ``scripts/ocr_with_mineru.py``;
they live here now so the annotator's per-block re-extract path (which
runs MinerU on a single cropped image) can reuse them without importing
the bulk script.
"""
from __future__ import annotations

import io
import json
import os
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from dotenv import load_dotenv

from pipeline.mineru import (
    MINERU_BLOCKS_DIR,
    MINERU_BLOCKS_FILENAME_SUFFIXES,
    MINERU_MARKDOWN_DIR,
    MINERU_RAW_ZIPS_DIR,
    MINERU_EXT_REMAP,
    safe_cache_name,
)


load_dotenv()

MINERU_API_KEY = os.environ.get("MINERU_API_KEY", "")
MINERU_BASE    = "https://mineru.net/api/v4"
MINERU_HEADERS = {
    "Authorization": f"Bearer {MINERU_API_KEY}" if MINERU_API_KEY else "",
    "Content-Type": "application/json",
}


class MinerUError(RuntimeError):
    """Raised when a MinerU API call fails or returns an unexpected payload."""


def _api_name(filename: str) -> str:
    """Apply ``MINERU_EXT_REMAP`` so we don't send unsupported extensions."""
    for src, tgt in MINERU_EXT_REMAP.items():
        if filename.lower().endswith(src):
            return filename[: -len(src)] + tgt
    return filename


def request_batch(items: list[dict],
                  *, model_version: str = "vlm") -> tuple[str, list[str]]:
    """POST /file-urls/batch.

    Each ``item`` needs ``filename`` (for the API-side name) and
    ``file_path`` (used to derive the ``data_id`` we match results by).
    Returns ``(batch_id, signed_urls)`` in the same order as ``items``.
    """
    if not MINERU_API_KEY:
        raise MinerUError("MINERU_API_KEY not set")
    payload = {
        "files": [
            {"name":    _api_name(it["filename"]),
             "data_id": safe_cache_name(it["file_path"])[:128]}
            for it in items
        ],
        "model_version": model_version,
    }
    r = requests.post(f"{MINERU_BASE}/file-urls/batch",
                      headers=MINERU_HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise MinerUError(f"MinerU batch request failed: {body}")
    data = body["data"]
    return data["batch_id"], data["file_urls"]


UPLOAD_CONCURRENCY = 8


def _put_one(it: dict, url: str) -> str | None:
    """Upload one file. Returns ``None`` on success or a short error string."""
    with open(it["disk_path"], "rb") as f:
        body = f.read()
    last_err: str | None = None
    for attempt in range(3):
        try:
            with requests.Session() as s:
                s.headers["Connection"] = "close"
                r = s.put(url, data=body, timeout=120)
            if r.ok:
                return None
            if 500 <= r.status_code < 600 and attempt < 2:
                last_err = f"HTTP {r.status_code}"
                time.sleep(2 ** attempt)
                continue
            return f"HTTP {r.status_code}"
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_err = f"{type(e).__name__}"
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
    return last_err


def upload_files(items: list[dict], signed_urls: list[str]) -> None:
    """PUT every item's bytes to its signed OSS URL, in parallel.

    Signed OSS URLs are short-lived; serial uploads on a 10-file batch
    can exceed the TTL on the last file and trigger HTTP 403. Running
    them in parallel keeps cumulative wall time at roughly one PUT
    duration regardless of batch size, well under any reasonable TTL.

    Per-file behaviour (in ``_put_one``):
    - fresh ``requests.Session`` (Connection: close) so a flaky TLS
      keep-alive on one PUT doesn't poison other uploads
    - 3-attempt retry with exponential backoff on SSL/connection/timeout
      errors and on 5xx responses; 4xx is logged once and skipped
    - failures don't raise out of ``upload_files``; the batch's other
      files still get to poll/download

    Concurrency: ``UPLOAD_CONCURRENCY`` threads (default 8). Tune lower
    if the egress proxy rate-limits.
    """
    workers = min(UPLOAD_CONCURRENCY, max(1, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(_put_one, it, url): it
            for it, url in zip(items, signed_urls)
        }
        for fut in as_completed(futs):
            it = futs[fut]
            try:
                err = fut.result()
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            if err:
                print(f"    [upload-fail] {it['filename']}: {err}")


def poll(batch_id: str, *, timeout_s: int = 600) -> list[dict]:
    """Block until every row in ``batch_id`` is ``done`` or ``failed``.

    Returns the rows list MinerU returned (each row has ``data_id``,
    ``state``, ``err_msg``, ``full_zip_url``).
    """
    poll_url = f"{MINERU_BASE}/extract-results/batch/{batch_id}"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = requests.get(poll_url, headers=MINERU_HEADERS, timeout=30)
        r.raise_for_status()
        rows = r.json().get("data", {}).get("extract_result", [])
        states = [row.get("state") for row in rows]
        if states and all(s in ("done", "failed") for s in states):
            return rows
        n_done    = sum(1 for s in states if s == "done")
        n_running = sum(1 for s in states if s == "running")
        n_pending = sum(1 for s in states if s == "pending")
        print(f"    [poll] done={n_done} running={n_running} pending={n_pending}",
              flush=True)
        time.sleep(8)
    raise TimeoutError(f"MinerU polling timeout after {timeout_s}s for batch {batch_id}")


def _find_blocks_member(z: zipfile.ZipFile) -> str | None:
    """Pick the first zip entry whose name matches one of our known
    content-list JSON suffixes."""
    names = z.namelist()
    for suffix in MINERU_BLOCKS_FILENAME_SUFFIXES:
        for n in names:
            if n.endswith(suffix):
                return n
    return None


def download_zip(full_zip_url: str, *, retries: int = 4) -> bytes | None:
    """Download a MinerU result zip with exponential-backoff retries.

    The signed OSS URL is short-lived but stable for a few minutes; we
    retry the whole download on network blips so a transient failure
    doesn't waste the upstream OCR work.
    """
    for attempt in range(retries):
        try:
            r = requests.get(full_zip_url, timeout=120)
            if r.ok:
                return r.content
            return None
        except requests.exceptions.RequestException as e:
            if attempt < retries - 1:
                wait = 2 ** attempt * 5
                print(f"    [zip-dl retry {attempt + 1}] "
                      f"{type(e).__name__}: {e}; waiting {wait}s")
                time.sleep(wait)
            else:
                print(f"    [zip-dl GAVE UP] {e}")
    return None


def parse_zip_payload(zip_bytes: bytes) -> tuple[str | None, list | None]:
    """Extract ``(markdown, raw_blocks_list)`` from a MinerU result zip.

    Either or both may be ``None`` if the zip is malformed or missing the
    expected files. ``raw_blocks_list`` is the parsed
    ``*_content_list.json`` array (caller normalizes with
    :func:`pipeline.mineru.parse_mineru_content_list`).
    """
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return None, None
    md_text: str | None = None
    blocks_raw: list | None = None
    if "full.md" in z.namelist():
        try:
            md_text = z.read("full.md").decode("utf-8")
        except (UnicodeDecodeError, KeyError):
            md_text = None
    blocks_member = _find_blocks_member(z)
    if blocks_member:
        try:
            blocks_raw = json.loads(z.read(blocks_member).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
            blocks_raw = None
    return md_text, blocks_raw


def download_and_cache(file_path: str, full_zip_url: str,
                       *, keep_zip: bool = False) -> tuple[Path | None, Path | None]:
    """Download the MinerU zip and write its payload to the on-disk caches.

    Caches written:
      - ``pipeline/cache/mineru_markdown/<safe>.md`` (the ``full.md`` text)
      - ``pipeline/cache/mineru_blocks/<safe>.json`` (the parsed content-list)
      - ``pipeline/cache/mineru_raw_zips/<safe>.zip`` if ``keep_zip=True``

    Returns ``(md_path, blocks_path)``. Either is ``None`` when that
    artifact was missing or unparseable.
    """
    MINERU_MARKDOWN_DIR.mkdir(parents=True, exist_ok=True)
    MINERU_BLOCKS_DIR.mkdir(parents=True, exist_ok=True)
    safe = safe_cache_name(file_path)
    md_path     = MINERU_MARKDOWN_DIR / f"{safe}.md"
    blocks_path = MINERU_BLOCKS_DIR   / f"{safe}.json"

    zip_bytes = download_zip(full_zip_url)
    if not zip_bytes:
        return None, None

    if keep_zip:
        MINERU_RAW_ZIPS_DIR.mkdir(parents=True, exist_ok=True)
        (MINERU_RAW_ZIPS_DIR / f"{safe}.zip").write_bytes(zip_bytes)

    md_text, blocks_raw = parse_zip_payload(zip_bytes)
    md_out: Path | None = None
    blocks_out: Path | None = None
    if md_text is not None:
        md_path.write_text(md_text, encoding="utf-8")
        md_out = md_path
    if blocks_raw is not None:
        # Persist raw MinerU output verbatim; downstream code normalizes.
        blocks_path.write_text(
            json.dumps(blocks_raw, ensure_ascii=False),
            encoding="utf-8",
        )
        blocks_out = blocks_path
    return md_out, blocks_out
