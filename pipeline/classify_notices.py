"""
pipeline/classify_notices.py
----------------------------
Classify each sale-notice file as single-property or multi-property.

Combines two signals:
  A. File sharing: if the same notice filename is referenced by 2+ auction_ids
     in pipeline/cache/ocr_results/, it is treated as a shared notice.
  B. LLM item count: a cheap vision call per unique file that asks how many
     distinct property items (Item No. / Sl. No. / Lot ...) the notice lists.

is_multi_property = (item_count > 1) OR (len(referenced_by) > 1)

Outputs:
  - pipeline/output/notice_classification.jsonl   (one row per unique notice file)
  - pipeline/cache/notice_classification/*.json   (per-file LLM result cache)
  - Optionally updates Document nodes in Neo4j with item_count / is_multi_property.

Run:
    python -m pipeline.classify_notices [--limit N] [--no-llm] [--no-neo4j]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from pipeline.config import (
    BATCH_SIZE,
    CACHE_DIR,
    DOWNLOADS_DIR,
    NEO4J_DATABASE,
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USERNAME,
    NOTICE_CLASS_CACHE_DIR,
    OUTPUT_DIR,
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


CLASSIFY_PROMPT = (PROMPTS_DIR / "classify_items.txt").read_text(encoding="utf-8")
REPORT_JSONL = OUTPUT_DIR / "notice_classification.jsonl"


# ── Signal A: file sharing across auctions ──────────────────────────────────

def build_sharing_map_from_cache() -> dict[str, list[str]]:
    """Walk the OCR cache and map each notice filename to the auction_ids that reference it.

    Cache filenames are shaped `{auction_id}__{notice_filename}.json`
    (see pipeline/ocr_extract.py:get_cache_path).
    """
    sharing: dict[str, set[str]] = defaultdict(set)
    for path in CACHE_DIR.glob("*.json"):
        stem = path.stem
        sep = stem.find("__")
        if sep <= 0:
            continue
        auction_id = stem[:sep]
        filename = stem[sep + 2:]
        sharing[filename].add(auction_id)
    return {fn: sorted(ids) for fn, ids in sharing.items()}


SHARING_MAP_CYPHER = """
MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
WHERE d.filename IS NOT NULL
WITH d.filename AS filename,
     collect(DISTINCT a.auction_id) AS auction_ids,
     collect(DISTINCT d.public_url)[0] AS public_url
RETURN filename, auction_ids, public_url
"""


# public_url cache: filename -> R2 URL, populated by the Neo4j sharing-map lookup
# so the Signal-B pass can download notices missing from local downloads/.
_PUBLIC_URLS: dict[str, str] = {}


def build_sharing_map_from_neo4j() -> dict[str, list[str]]:
    """Query Neo4j Documents + AuctionProperty edges to build the filename -> auction_ids map.

    Also captures each file's public_url so Signal B can fall back to R2 fetch.
    """
    if not (NEO4J_URI and NEO4J_USERNAME and NEO4J_PASSWORD):
        return {}
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    try:
        with driver.session(database=NEO4J_DATABASE) as s:
            rows = list(s.run(SHARING_MAP_CYPHER))
    finally:
        driver.close()
    out: dict[str, list[str]] = {}
    for r in rows:
        fn = r["filename"]
        if not fn:
            continue
        out[fn] = sorted(r["auction_ids"])
        if r["public_url"]:
            _PUBLIC_URLS[fn] = r["public_url"]
    return out


def build_sharing_map() -> dict[str, list[str]]:
    """Prefer the local OCR cache; fall back to Neo4j when the cache is empty.

    The cache is present after a local OCR run; on a fresh CI runner it will be
    empty, so we read the same sharing relation directly from the graph.
    """
    from_cache = build_sharing_map_from_cache()
    if from_cache:
        print(f"  Sharing map source: OCR cache ({len(from_cache)} files)")
        return from_cache
    from_neo4j = build_sharing_map_from_neo4j()
    if from_neo4j:
        print(f"  Sharing map source: Neo4j ({len(from_neo4j)} files)")
    else:
        print("  [WARN] OCR cache empty and Neo4j unreachable; sharing map is empty.")
    return from_neo4j


def resolve_notice_path(filename: str) -> Path | None:
    """Resolve the on-disk path for a notice filename under DOWNLOADS_DIR."""
    direct = DOWNLOADS_DIR / filename
    if direct.exists():
        return direct
    matches = list(DOWNLOADS_DIR.glob(f"*{filename}*"))
    return matches[0] if matches else None


# ── Signal B: LLM item count ────────────────────────────────────────────────

def class_cache_path(filename: str) -> Path:
    safe = filename.replace("/", "_").replace("\\", "_")
    return NOTICE_CLASS_CACHE_DIR / f"{safe}.json"


def read_class_cache(filename: str) -> dict | None:
    path = class_cache_path(filename)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_class_cache(filename: str, result: dict) -> None:
    class_cache_path(filename).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def _fetch_from_r2(session: aiohttp.ClientSession, url: str, suffix: str) -> Path | None:
    import os
    import tempfile

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


async def classify_one(
    filename: str,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
) -> dict | None:
    """Return {item_count, item_markers, confidence, reasoning} for a notice file."""
    cached = read_class_cache(filename)
    if cached is not None:
        return cached

    file_path = resolve_notice_path(filename)
    tmp_path: Path | None = None
    if file_path is None:
        url = _PUBLIC_URLS.get(filename)
        if not url:
            return None
        suffix = Path(filename).suffix or ".bin"
        file_path = await _fetch_from_r2(session, url, suffix)
        if file_path is None:
            return None
        tmp_path = file_path

    try:
        ext = file_path.suffix.lower()
        b64_images: list[tuple[str, str]] = []
        if ext in IMAGE_EXTS:
            b64 = encode_image_to_base64(file_path)
            if b64:
                b64_images.append((b64, get_mime_type(ext)))
        elif ext in PDF_EXTS:
            b64_images = pdf_to_images(file_path)
        else:
            return None

        if not b64_images:
            return None

        result = await call_vision_api(session, b64_images, CLASSIFY_PROMPT, semaphore)
        if result is None:
            return None

        normalized = {
            "item_count":   int(result.get("item_count") or 0),
            "item_markers": list(result.get("item_markers") or []),
            "confidence":   result.get("confidence") or "low",
            "reasoning":    result.get("reasoning") or "",
        }
        write_class_cache(filename, normalized)
        return normalized
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


# ── Combine signals, write report ───────────────────────────────────────────

def combine(filename: str, referenced_by: list[str], llm: dict | None) -> dict:
    item_count = llm["item_count"] if llm and llm.get("item_count") else None
    item_markers = llm.get("item_markers") if llm else []
    confidence = llm.get("confidence") if llm else None
    reasoning = llm.get("reasoning") if llm else None

    is_multi = False
    if item_count is not None and item_count > 1:
        is_multi = True
    if len(referenced_by) > 1:
        is_multi = True

    return {
        "filename":          filename,
        "item_count":        item_count,
        "item_markers":      item_markers,
        "confidence":        confidence,
        "reasoning":         reasoning,
        "referenced_by":     referenced_by,
        "referenced_count":  len(referenced_by),
        "is_multi_property": is_multi,
        "classification":    "multi_property" if is_multi else "single_property",
        "classified_at":     datetime.now(timezone.utc).isoformat(),
    }


async def run_llm_pass(filenames: list[str]) -> dict[str, dict]:
    """Classify each filename with the vision LLM, concurrently."""
    semaphore = asyncio.Semaphore(BATCH_SIZE)
    connector = aiohttp.TCPConnector(limit=BATCH_SIZE * 2)
    out: dict[str, dict] = {}
    t0 = time.time()
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [asyncio.create_task(classify_one(fn, session, semaphore)) for fn in filenames]
        done = 0
        for fn, coro in zip(filenames, tasks):
            try:
                result = await coro
            except Exception as e:
                print(f"  [ERROR] {fn}: {e}")
                result = None
            if result is not None:
                out[fn] = result
            done += 1
            if done % 10 == 0 or done == len(filenames):
                rate = done / (time.time() - t0) if time.time() > t0 else 0.0
                print(f"  [{done}/{len(filenames)}] {rate:.1f} file/s", end="\r")
            await asyncio.sleep(RATE_LIMIT_DELAY / max(BATCH_SIZE, 1))
    print()
    return out


# ── Neo4j update ────────────────────────────────────────────────────────────

NEO4J_UPDATE_QUERY = """
UNWIND $rows AS r
MATCH (d:Document {filename: r.filename})
SET d.item_count        = r.item_count,
    d.item_markers      = r.item_markers,
    d.is_multi_property = r.is_multi_property,
    d.classification    = r.classification,
    d.classified_at     = datetime(r.classified_at)
RETURN count(d) AS updated
"""


def update_neo4j(rows: list[dict]) -> int:
    if not rows:
        return 0
    if not (NEO4J_URI and NEO4J_USERNAME and NEO4J_PASSWORD):
        print("  [WARN] Neo4j credentials missing; skipping DB update.")
        return 0

    from neo4j import GraphDatabase  # local import so --no-neo4j runs without the driver

    payload = [
        {
            "filename":          r["filename"],
            "item_count":        r["item_count"],
            "item_markers":      r["item_markers"],
            "is_multi_property": r["is_multi_property"],
            "classification":    r["classification"],
            "classified_at":     r["classified_at"],
        }
        for r in rows
    ]

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    try:
        with driver.session(database=NEO4J_DATABASE) as s:
            result = s.run(NEO4J_UPDATE_QUERY, rows=payload)
            return int(result.single()["updated"])
    finally:
        driver.close()


# ── Main ────────────────────────────────────────────────────────────────────

def run(limit: int | None, use_llm: bool, use_neo4j: bool) -> None:
    sharing = build_sharing_map()
    filenames = sorted(sharing.keys())
    if limit:
        filenames = filenames[:limit]

    total = len(filenames)
    shared = sum(1 for fn in filenames if len(sharing[fn]) > 1)
    print(f"Notice classification: {total} unique notice file(s)")
    print(f"  Shared across 2+ auctions (Signal A): {shared}")

    llm_results: dict[str, dict] = {}
    if use_llm:
        resolvable = [
            fn for fn in filenames
            if resolve_notice_path(fn) is not None or _PUBLIC_URLS.get(fn)
        ]
        missing = total - len(resolvable)
        if missing:
            print(f"  [WARN] {missing} notice file(s) not found locally and have no public_url; skipping LLM for those.")
        print(f"  Running LLM item-count on {len(resolvable)} file(s) (local + R2 fetch)...")
        llm_results = asyncio.run(run_llm_pass(resolvable))
    else:
        print("  Skipping LLM pass (--no-llm).")

    rows = [combine(fn, sharing[fn], llm_results.get(fn)) for fn in filenames]

    REPORT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_JSONL, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    multi = sum(1 for r in rows if r["is_multi_property"])
    print(f"\n  Written: {len(rows)} rows -> {REPORT_JSONL}")
    print(f"  Multi-property: {multi} | Single-property: {len(rows) - multi}")

    if use_neo4j:
        print("  Updating Neo4j Document nodes...")
        updated = update_neo4j(rows)
        print(f"  Neo4j updated: {updated} Document node(s)")
    else:
        print("  Skipping Neo4j update (--no-neo4j).")


def main():
    parser = argparse.ArgumentParser(description="Classify sale notices as single or multi-property")
    parser.add_argument("--limit", type=int, default=None, help="Process only first N unique notice files")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM pass, use file-sharing signal only")
    parser.add_argument("--no-neo4j", action="store_true", help="Skip Neo4j update; write report only")
    args = parser.parse_args()
    run(limit=args.limit, use_llm=not args.no_llm, use_neo4j=not args.no_neo4j)


if __name__ == "__main__":
    main()
