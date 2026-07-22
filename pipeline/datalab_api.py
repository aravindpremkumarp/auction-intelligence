"""Datalab hosted-convert API client (the Marker/Surya engine).

Sibling to :mod:`pipeline.mineru_api`. Used by the OCR A/B harness
(``scripts/ocr_ab.py``) to run Datalab against MinerU on real notices without
touching the production pipeline. Kept deliberately small — submit, poll, and
pull the payload apart.

Flow (per file):

  1. ``POST /api/v1/convert`` (multipart: ``file`` + params)
       -> ``{success, request_id, request_check_url}``
  2. ``GET request_check_url`` (same ``X-API-Key`` header) until
       ``status == "complete"`` (or ``"failed"``)
  3. pull ``markdown`` / ``json`` (the block tree) / ``images`` out of the
     completed payload

Auth: ``DATALAB_API_KEY`` in ``.env``, sent as the ``X-API-Key`` header.

``mode`` defaults to ``"fast"`` — Marker's lowest-latency tier (the one
validated in the Datalab playground). Bump to ``"balanced"`` / ``"accurate"``
for harder documents at higher cost/latency.
"""
from __future__ import annotations

import json
import mimetypes
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv


load_dotenv()

DATALAB_API_KEY     = os.environ.get("DATALAB_API_KEY", "")
DATALAB_BASE        = "https://www.datalab.to/api/v1"
DATALAB_CONVERT_URL = f"{DATALAB_BASE}/convert"
DEFAULT_MODE        = "fast"

# Terminal poll states. Anything else (e.g. "processing") means keep polling.
_DONE_STATES   = {"complete"}
_FAILED_STATES = {"failed", "error"}


class DatalabError(RuntimeError):
    """Raised when a Datalab API call fails or returns an unexpected payload."""


def _headers() -> dict[str, str]:
    if not DATALAB_API_KEY:
        raise DatalabError("DATALAB_API_KEY not set")
    return {"X-API-Key": DATALAB_API_KEY}


def _content_type(path: Path) -> str:
    ct, _ = mimetypes.guess_type(str(path))
    return ct or "application/octet-stream"


def submit(disk_path: str | Path, *, output_format: str = "json",
           mode: str = DEFAULT_MODE,
           extra: dict[str, str] | None = None) -> tuple[str, str]:
    """POST one file to ``/convert``. Returns ``(request_id, check_url)``.

    ``output_format`` is ``json`` (block tree with bboxes), ``markdown``,
    ``html`` or ``chunks``. ``extra`` passes any additional multipart fields
    verbatim (``max_pages``, ``page_range``, ``disable_image_extraction`` …).
    """
    path = Path(disk_path)
    data: dict[str, str] = {"output_format": output_format, "mode": mode}
    if extra:
        data.update({k: str(v) for k, v in extra.items()})
    with open(path, "rb") as f:
        files = {"file": (path.name, f, _content_type(path))}
        r = requests.post(DATALAB_CONVERT_URL, headers=_headers(),
                          files=files, data=data, timeout=60)
    r.raise_for_status()
    body = r.json()
    if not body.get("success", False):
        raise DatalabError(f"Datalab submit failed: {body.get('error') or body}")
    check_url = body.get("request_check_url")
    if not check_url:
        raise DatalabError(f"Datalab submit returned no request_check_url: {body}")
    return body.get("request_id", ""), check_url


def poll(check_url: str, *, timeout_s: int = 300,
         interval_s: float = 3.0, max_interval_s: float = 10.0) -> dict:
    """Poll ``check_url`` until the job completes; return the final payload.

    Raises :class:`DatalabError` on a failed job and ``TimeoutError`` if the
    job hasn't finished within ``timeout_s``. Backs off from ``interval_s`` up
    to ``max_interval_s`` between polls.
    """
    deadline = time.time() + timeout_s
    wait = interval_s
    while time.time() < deadline:
        r = requests.get(check_url, headers=_headers(), timeout=30)
        r.raise_for_status()
        body = r.json()
        status = str(body.get("status", "")).lower()
        if status in _DONE_STATES:
            if body.get("success") is False:
                raise DatalabError(f"Datalab job failed: {body.get('error') or body}")
            return body
        if status in _FAILED_STATES or body.get("success") is False:
            raise DatalabError(f"Datalab job failed: {body.get('error') or body}")
        time.sleep(wait)
        wait = min(wait * 1.5, max_interval_s)
    raise TimeoutError(f"Datalab polling timeout after {timeout_s}s for {check_url}")


def run_file(disk_path: str | Path, *, output_format: str = "json",
             mode: str = DEFAULT_MODE, timeout_s: int = 300,
             extra: dict[str, str] | None = None) -> dict:
    """Submit + poll convenience. Returns the completed response payload."""
    _rid, check_url = submit(disk_path, output_format=output_format,
                             mode=mode, extra=extra)
    return poll(check_url, timeout_s=timeout_s)


def extract_payload(result: dict) -> tuple[str | None, object, dict]:
    """Split a completed payload into ``(markdown, json_block_tree, images)``.

    Any field absent for the requested ``output_format`` comes back as
    ``None`` / ``{}``. ``images`` is the ``{name: base64}`` map Datalab emits
    for extracted figures/tables.
    """
    return (result.get("markdown"),
            result.get("json"),
            result.get("images") or {})


def run_and_cache(file_path: str, disk_path: str | Path, *,
                  mode: str = DEFAULT_MODE, timeout_s: int = 420) -> tuple[Path, Path]:
    """Run Datalab on ``disk_path`` and cache markdown + blocks for the pipeline.

    Writes into the SAME on-disk cache the MinerU path uses so the Neo4j loader
    reads Datalab output with no changes:

      - ``mineru_markdown/<safe>.md``   — the markdown (native, else assembled)
      - ``mineru_blocks/<safe>.json``   — ``{"blocks": [<canonical blocks>], ...}``

    The blocks are stored pre-normalized (canonical shape, ``source="datalab"``)
    wrapped in a dict, which ``load_markdowns_to_neo4j.load_blocks_for`` already
    treats as "already normalized" (it assigns ids and skips re-parsing). Returns
    ``(md_path, blocks_path)``.
    """
    # Local imports: keep this module importable without pulling the mineru
    # cache constants / parser at import time, and avoid any import-order edge.
    from pipeline.datalab import parse_datalab_blocks
    from pipeline.mineru import (
        MINERU_BLOCKS_DIR, MINERU_MARKDOWN_DIR, assemble_markdown, safe_cache_name,
    )

    result = run_file(disk_path, output_format="json", mode=mode, timeout_s=timeout_s)
    md, doc, _images = extract_payload(result)
    blocks = parse_datalab_blocks(doc)
    markdown = md or assemble_markdown(blocks)

    MINERU_MARKDOWN_DIR.mkdir(parents=True, exist_ok=True)
    MINERU_BLOCKS_DIR.mkdir(parents=True, exist_ok=True)
    safe = safe_cache_name(file_path)
    md_path = MINERU_MARKDOWN_DIR / f"{safe}.md"
    bl_path = MINERU_BLOCKS_DIR / f"{safe}.json"
    md_path.write_text(markdown or "", encoding="utf-8")
    bl_path.write_text(
        json.dumps({"blocks": blocks, "engine": "datalab", "mode": mode},
                   ensure_ascii=False),
        encoding="utf-8")
    return md_path, bl_path
