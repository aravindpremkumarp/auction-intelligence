"""
pipeline/extract_property_items.py
----------------------------------
Stage 2a: for every sale-notice flagged as multi-property, extract one record
per property item (borrower, reserve price, EMD, survey #s, verbatim item text,
page number). Results are cached per-file and written back to the Document node
as `property_items_json`.

Source of notices:
  - Neo4j Document nodes with `is_multi_property = true`
    (produced by pipeline/classify_notices.py), OR
  - Fallback: Documents whose filename is shared by >=2 AuctionProperty nodes.
    (Lets this run even before classify_notices has been executed.)

Source of raw files:
  - Local `downloads/` first, then fall back to `doc.public_url` (R2).

Run:
    python -m pipeline.extract_property_items [--limit N] [--force]
    python -m pipeline.extract_property_items --filenames a.pdf b.pdf
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from pipeline.config import (
    BATCH_SIZE,
    DOWNLOADS_DIR,
    NEO4J_DATABASE,
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USERNAME,
    OUTPUT_DIR,
    PIPELINE_DIR,
    PROMPTS_DIR,
    RATE_LIMIT_DELAY,
)
from pipeline.ocr_extract import (
    IMAGE_EXTS,
    PDF_EXTS,
    call_vision_api,
    encode_image_to_base64,
    get_mime_type,
    pdf_to_images,
)


ITEMS_CACHE_DIR = PIPELINE_DIR / "cache" / "property_items"
ITEMS_CACHE_DIR.mkdir(parents=True, exist_ok=True)

ITEMS_REPORT = OUTPUT_DIR / "property_items.jsonl"

PROMPT = (PROMPTS_DIR / "extract_property_items.txt").read_text(encoding="utf-8")


# ── Pick notices to extract from Neo4j ──────────────────────────────────────

SELECT_MULTI_PROP_CLASSIFIED = """
MATCH (d:Document) WHERE d.is_multi_property = true AND d.filename IS NOT NULL
RETURN DISTINCT d.filename AS filename, d.public_url AS public_url
"""

SELECT_SHARED_FALLBACK = """
MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
WHERE d.filename IS NOT NULL
WITH d.filename AS filename, collect(DISTINCT a.auction_id) AS auction_ids,
     collect(DISTINCT d.public_url)[0] AS public_url
WHERE size(auction_ids) > 1
RETURN filename, public_url
"""


def list_multi_property_notices() -> list[dict]:
    """Return [{filename, public_url}] for notices we need to extract items from."""
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    try:
        with driver.session(database=NEO4J_DATABASE) as s:
            rows = list(s.run(SELECT_MULTI_PROP_CLASSIFIED))
            if not rows:
                print("  [INFO] No documents flagged is_multi_property; falling back to shared-notice heuristic.")
                rows = list(s.run(SELECT_SHARED_FALLBACK))
            return [{"filename": r["filename"], "public_url": r["public_url"]} for r in rows]
    finally:
        driver.close()


# ── Fetch raw file ───────────────────────────────────────────────────────────

def resolve_local(filename: str) -> Path | None:
    direct = DOWNLOADS_DIR / filename
    if direct.exists():
        return direct
    matches = list(DOWNLOADS_DIR.glob(f"*{filename}*"))
    return matches[0] if matches else None


async def fetch_from_url(session: aiohttp.ClientSession, url: str, suffix: str) -> Path | None:
    """Download a notice from R2 to a temp file, return its path."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                print(f"  [WARN] fetch {url} -> HTTP {resp.status}")
                return None
            data = await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"  [WARN] fetch {url}: {e}")
        return None

    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    Path(tmp_path).write_bytes(data)
    return Path(tmp_path)


# ── Cache ────────────────────────────────────────────────────────────────────

def cache_path(filename: str) -> Path:
    safe = filename.replace("/", "_").replace("\\", "_")
    return ITEMS_CACHE_DIR / f"{safe}.json"


def read_cache(filename: str) -> dict | None:
    p = cache_path(filename)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_cache(filename: str, payload: dict) -> None:
    cache_path(filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Extraction ───────────────────────────────────────────────────────────────

async def prepare_images(
    session: aiohttp.ClientSession, filename: str, public_url: str | None
) -> tuple[list[tuple[str, str]], Path | None]:
    """Return (base64_images, tmp_path_to_cleanup)."""
    file_path = resolve_local(filename)
    tmp_path: Path | None = None
    if file_path is None:
        if not public_url:
            return [], None
        suffix = Path(filename).suffix or ".bin"
        file_path = await fetch_from_url(session, public_url, suffix)
        if file_path is None:
            return [], None
        tmp_path = file_path

    ext = file_path.suffix.lower()
    if ext in IMAGE_EXTS:
        b64 = encode_image_to_base64(file_path)
        return ([(b64, get_mime_type(ext))] if b64 else []), tmp_path
    if ext in PDF_EXTS:
        return pdf_to_images(file_path), tmp_path
    return [], tmp_path


async def extract_one(
    notice: dict,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    force: bool,
) -> dict | None:
    filename = notice["filename"]
    if not force:
        cached = read_cache(filename)
        if cached is not None:
            return cached

    imgs, tmp = await prepare_images(session, filename, notice.get("public_url"))
    try:
        if not imgs:
            return None
        result = await call_vision_api(session, imgs, PROMPT, semaphore)
        if result is None:
            return None
        items = result.get("property_items") or []
        # Defensive: stamp page_number to 1 when missing for single-image notices
        if len(imgs) == 1:
            for it in items:
                if it.get("page_number") in (None, 0):
                    it["page_number"] = 1
        payload = {
            "filename":       filename,
            "item_count":     len(items),
            "property_items": items,
            "extracted_at":   datetime.now(timezone.utc).isoformat(),
        }
        write_cache(filename, payload)
        return payload
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


async def run_extraction(notices: list[dict], force: bool) -> list[dict]:
    semaphore = asyncio.Semaphore(BATCH_SIZE)
    connector = aiohttp.TCPConnector(limit=BATCH_SIZE * 2)
    results: list[dict] = []
    t0 = time.time()
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [asyncio.create_task(extract_one(n, session, semaphore, force)) for n in notices]
        done = 0
        for coro in tasks:
            try:
                r = await coro
            except Exception as e:
                print(f"  [ERROR] {e}")
                r = None
            if r is not None:
                results.append(r)
            done += 1
            rate = done / (time.time() - t0) if time.time() > t0 else 0.0
            print(f"  [{done}/{len(tasks)}] {rate:.1f} file/s | got items for {len(results)}", end="\r")
            await asyncio.sleep(RATE_LIMIT_DELAY / max(BATCH_SIZE, 1))
    print()
    return results


# ── Neo4j write-back ─────────────────────────────────────────────────────────

WRITE_BACK = """
UNWIND $rows AS r
MATCH (d:Document {filename: r.filename})
SET d.property_items_json = r.property_items_json,
    d.item_count          = r.item_count,
    d.is_multi_property   = r.is_multi_property,
    d.items_extracted_at  = datetime(r.extracted_at)
RETURN count(d) AS updated
"""


def update_neo4j(results: list[dict]) -> int:
    if not results:
        return 0
    from neo4j import GraphDatabase

    payload = [
        {
            "filename":            r["filename"],
            "property_items_json": json.dumps(r.get("property_items") or [], ensure_ascii=False),
            "item_count":          r.get("item_count") or 0,
            "is_multi_property":   (r.get("item_count") or 0) > 1,
            "extracted_at":        r["extracted_at"],
        }
        for r in results
    ]
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    try:
        with driver.session(database=NEO4J_DATABASE) as s:
            out = s.run(WRITE_BACK, rows=payload)
            return int(out.single()["updated"])
    finally:
        driver.close()


# ── Main ─────────────────────────────────────────────────────────────────────

def run(limit: int | None, force: bool, filenames: list[str] | None, no_neo4j: bool) -> None:
    if filenames:
        notices = [{"filename": fn, "public_url": None} for fn in filenames]
        print(f"Property-item extraction: {len(notices)} notice(s) from --filenames")
    else:
        notices = list_multi_property_notices()
        print(f"Property-item extraction: {len(notices)} multi-property notice(s) in Neo4j")

    if limit:
        notices = notices[:limit]

    results = asyncio.run(run_extraction(notices, force=force))

    ITEMS_REPORT.parent.mkdir(parents=True, exist_ok=True)
    with open(ITEMS_REPORT, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    total_items = sum(r.get("item_count") or 0 for r in results)
    print(f"  Extracted items: {total_items} across {len(results)} notice(s)")
    print(f"  Report: {ITEMS_REPORT}")

    if no_neo4j:
        print("  Skipping Neo4j write-back (--no-neo4j).")
        return

    if not (NEO4J_URI and NEO4J_USERNAME and NEO4J_PASSWORD):
        print("  [WARN] Neo4j credentials missing; skipping DB write-back.")
        return

    updated = update_neo4j(results)
    print(f"  Neo4j updated: {updated} Document node(s)")


def main():
    p = argparse.ArgumentParser(description="Extract per-item records from multi-property sale notices")
    p.add_argument("--limit", type=int, default=None, help="Process only first N notices")
    p.add_argument("--force", action="store_true", help="Ignore per-file cache and re-run LLM")
    p.add_argument("--filenames", nargs="*", default=None, help="Override: process only these notice filenames")
    p.add_argument("--no-neo4j", action="store_true", help="Skip Neo4j write-back; write report only")
    args = p.parse_args()
    run(limit=args.limit, force=args.force, filenames=args.filenames, no_neo4j=args.no_neo4j)


if __name__ == "__main__":
    main()
