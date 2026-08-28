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

# Statuses worth retrying rather than failing: rate-limit + transient upstream.
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_SUBMIT_RETRIES = 6

# Marker EXCLUDES page headers and footers from its output by default: the
# block still comes back in the JSON tree, with its bbox and an EMPTY ``html``.
# On these notices that silently drops the letterhead (bank name, CIN, regd.
# office, contact) and the sign-off (authorised officer, place, date) — real
# notice content, not running heads, because a one-page clipping's "page
# header" IS the letterhead.
#
# Worse, it costs health twice over: the text is gone from the markdown, and
# because the empty block covers no ink, ``pipeline/ink_coverage.py`` counts
# the whole band as unread and the doc picks up ``missing-region`` (-45).
# Measured on 08227169-…jpg: unread ink 15.9% -> 1.3%, health 55 -> 100 with
# these on, and the 5 empty Header + 2 empty Footer blocks come back carrying
# their text.
#
# The keys ONLY take effect inside ``additional_config`` (a JSON string) —
# passed as top-level form fields the API accepts the request and ignores
# them, which reads exactly like the feature not existing.
DEFAULT_ADDITIONAL_CONFIG = {
    "keep_pageheader_in_output": True,
    "keep_pagefooter_in_output": True,
}


class DatalabError(RuntimeError):
    """Raised when a Datalab API call fails or returns an unexpected payload."""


def _retry_after(resp, attempt: int, *, base: float = 2.0, cap: float = 60.0) -> float:
    """Seconds to wait before a retry — honour a ``Retry-After`` header if the
    server sent one, else exponential backoff (base * 2**attempt, capped)."""
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return min(cap, float(ra))
        except ValueError:
            pass
    return min(cap, base * (2 ** attempt))


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

    Every submit carries :data:`DEFAULT_ADDITIONAL_CONFIG` so page headers and
    footers keep their text (see that constant). A caller that passes its own
    ``additional_config`` in ``extra`` REPLACES it wholesale — merge the
    defaults in yourself if you still want them.
    """
    path = Path(disk_path)
    data: dict[str, str] = {
        "output_format": output_format,
        "mode": mode,
        "additional_config": json.dumps(DEFAULT_ADDITIONAL_CONFIG),
    }
    if extra:
        data.update({k: str(v) for k, v in extra.items()})
    last_status: int | None = None
    for attempt in range(_MAX_SUBMIT_RETRIES):
        # Re-open per attempt: the multipart body is consumed on each POST.
        with open(path, "rb") as f:
            files = {"file": (path.name, f, _content_type(path))}
            r = requests.post(DATALAB_CONVERT_URL, headers=_headers(),
                              files=files, data=data, timeout=60)
        if r.status_code in _RETRY_STATUSES and attempt < _MAX_SUBMIT_RETRIES - 1:
            last_status = r.status_code
            time.sleep(_retry_after(r, attempt))
            continue
        r.raise_for_status()
        body = r.json()
        if not body.get("success", False):
            raise DatalabError(f"Datalab submit failed: {body.get('error') or body}")
        check_url = body.get("request_check_url")
        if not check_url:
            raise DatalabError(f"Datalab submit returned no request_check_url: {body}")
        return body.get("request_id", ""), check_url
    raise DatalabError(f"Datalab submit rate-limited/unavailable after "
                       f"{_MAX_SUBMIT_RETRIES} attempts (last status {last_status})")


def poll(check_url: str, *, timeout_s: int = 300,
         interval_s: float = 3.0, max_interval_s: float = 10.0) -> dict:
    """Poll ``check_url`` until the job completes; return the final payload.

    Raises :class:`DatalabError` on a failed job and ``TimeoutError`` if the
    job hasn't finished within ``timeout_s``. Backs off from ``interval_s`` up
    to ``max_interval_s`` between polls.
    """
    deadline = time.time() + timeout_s
    wait = interval_s
    rl_attempt = 0
    while time.time() < deadline:
        r = requests.get(check_url, headers=_headers(), timeout=30)
        if r.status_code in _RETRY_STATUSES:
            # Rate-limited / transient: back off and keep polling, don't fail.
            time.sleep(_retry_after(r, rl_attempt))
            rl_attempt += 1
            continue
        rl_attempt = 0
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


def parse_quality(result: dict) -> float | None:
    """Datalab's own verdict on the parse, 0–5 (higher is better), or ``None``.

    This is the engine's self-assessment of how faithfully it read the page —
    the signal ``pipeline.ocr_health`` structurally cannot give, since that
    module only inspects the text we *did* get and never sees the image. A
    notice with a third of its content silently dropped still scores 100 on
    health while Datalab rates the parse ~3/5.

    Returns ``None`` when the field is absent or non-numeric. The common cause
    is a **cache hit**: Datalab replays a previous conversion (``runtime`` ≈ 0,
    ``total_cost`` 0) without re-scoring it, so ``parse_quality_score`` comes
    back null. Submit with ``skip_cache="true"`` when the score matters.
    """
    v = result.get("parse_quality_score")
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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

    The sidecar also carries ``parse_quality_score`` (see :func:`parse_quality`)
    so ``pipeline.load_markdowns_to_neo4j`` can stamp it on the Document
    alongside the blocks.

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
        json.dumps({"blocks": blocks, "engine": "datalab", "mode": mode,
                    "parse_quality_score": parse_quality(result)},
                   ensure_ascii=False),
        encoding="utf-8")
    return md_path, bl_path
