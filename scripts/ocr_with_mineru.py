"""Phase A full rollout: re-OCR every TN notice with MinerU API + LLM.

Pipeline:
  notice file (jpg/png/pdf)
    -> MinerU API (https://mineru.net/api/v4) — vlm model, batched in groups of 20
       1. POST /file-urls/batch  -> batch_id + signed OSS upload URLs
       2. PUT file content to each signed URL
       3. Poll GET /extract-results/batch/<batch_id> until done
       4. Download full_zip_url, extract full.md
    -> Gemini 2.0 Flash text-only via OpenRouter (single-property notices only)
       -> property_description_full
    -> Apply v3 descriptions to Neo4j AuctionProperty.description with
       description_source='notice'

Resumable:
  - MinerU markdown cached at pipeline/cache/mineru_markdown/<safe_path>.md
  - v3 description cached at pipeline/cache/notice_descriptions_v3/<safe_path>.json
  - Re-runs skip cached entries

Usage:
  python -m scripts.ocr_with_mineru               # full run
  python -m scripts.ocr_with_mineru --limit 50    # cap to first 50 Documents
  python -m scripts.ocr_with_mineru --skip-mineru # only run LLM + apply stages
  python -m scripts.ocr_with_mineru --skip-apply  # don't write to Neo4j

Auth: MINERU_API_KEY + OPENROUTER_API_KEY in .env
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import aiohttp
import requests
from dotenv import load_dotenv

from api.neo4j_client import run_query, run_read_query
from pipeline.config import (
    OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL,
    PROMPTS_DIR, MAX_RETRIES,
)


load_dotenv()
MINERU_KEY = os.environ.get("MINERU_API_KEY")
MINERU_BASE = "https://mineru.net/api/v4"
MINERU_HEADERS = {
    "Authorization": f"Bearer {MINERU_KEY}" if MINERU_KEY else "",
    "Content-Type": "application/json",
}

REPO_ROOT          = Path(__file__).resolve().parent.parent
MINERU_MD_DIR      = REPO_ROOT / "pipeline" / "cache" / "mineru_markdown"
NOTICE_DESC_V3_DIR = REPO_ROOT / "pipeline" / "cache" / "notice_descriptions_v3"
PROMPT_PATH        = PROMPTS_DIR / "extract_description.txt"

MINERU_BATCH_SIZE = 20      # files per MinerU batch request
LLM_CONCURRENCY   = 6       # concurrent OpenRouter calls
WRITE_CHUNK       = 200     # rows per UNWIND Cypher write

# MinerU accepts: pdf, jpg, jpeg, png. .jfif IS jpeg under the hood but the
# API rejects it on extension. We remap .jfif -> .jpg in the request `name`
# field; the bytes are unchanged.
MINERU_SUPPORTED_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".jfif"}
MINERU_EXT_REMAP = {".jfif": ".jpg"}  # extension -> what to claim it is in the API call


# ── helpers ──────────────────────────────────────────────────────────────────

def safe_cache_name(file_path: str) -> str:
    return file_path.replace('/', '_').replace('\\', '_').replace(':', '_')


def find_disk_path(filename: str) -> Path | None:
    for base in (REPO_ROOT / "downloads" / "tn_properties",
                 REPO_ROOT / "downloads"):
        p = base / filename
        if p.exists():
            return p
    return None


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_all_work() -> list[dict]:
    """Every Document + its linked listings, with notice_type for routing."""
    return run_read_query("""
      MATCH (d:Document)
      OPTIONAL MATCH (d)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
      WITH d, collect(a.auction_id) AS aids
      RETURN d.filename       AS filename,
             d.file_path      AS file_path,
             coalesce(d.notice_type, 'unknown') AS notice_type,
             aids
    """, max_rows=10_000)


# ── MinerU stage ─────────────────────────────────────────────────────────────

def mineru_request_batch(items: list[dict]) -> tuple[str, list[str]]:
    def api_name(filename: str) -> str:
        # Remap unsupported-but-equivalent extensions (e.g. .jfif -> .jpg)
        for src, tgt in MINERU_EXT_REMAP.items():
            if filename.lower().endswith(src):
                return filename[: -len(src)] + tgt
        return filename

    payload = {
        "files": [{"name": api_name(it["filename"]),
                   "data_id": safe_cache_name(it["file_path"])[:128]}
                  for it in items],
        "model_version": "vlm",
    }
    r = requests.post(f"{MINERU_BASE}/file-urls/batch",
                      headers=MINERU_HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"MinerU batch request failed: {body}")
    data = body["data"]
    return data["batch_id"], data["file_urls"]


def mineru_upload_files(items: list[dict], signed_urls: list[str]) -> None:
    for it, url in zip(items, signed_urls):
        with open(it["disk_path"], "rb") as f:
            r = requests.put(url, data=f.read(), timeout=120)
        if not r.ok:
            print(f"    [upload-fail] {it['filename']}: HTTP {r.status_code}")


def mineru_poll(batch_id: str, timeout_s: int = 600) -> list[dict]:
    poll_url = f"{MINERU_BASE}/extract-results/batch/{batch_id}"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = requests.get(poll_url, headers=MINERU_HEADERS, timeout=30)
        r.raise_for_status()
        rows = r.json().get("data", {}).get("extract_result", [])
        states = [r.get("state") for r in rows]
        if states and all(s in ("done", "failed") for s in states):
            return rows
        n_done = sum(1 for s in states if s == "done")
        n_running = sum(1 for s in states if s == "running")
        n_pending = sum(1 for s in states if s == "pending")
        print(f"    [poll] done={n_done} running={n_running} pending={n_pending}", flush=True)
        time.sleep(8)
    raise TimeoutError(f"MinerU polling timeout after {timeout_s}s for batch {batch_id}")


def download_and_cache_md(file_path: str, full_zip_url: str) -> Path | None:
    MINERU_MD_DIR.mkdir(parents=True, exist_ok=True)
    md_path = MINERU_MD_DIR / f"{safe_cache_name(file_path)}.md"
    # Retry on transient network errors (ConnectionResetError, timeouts) —
    # the OSS download URL is short-lived but stable for the few minutes after
    # MinerU returns it, so a few retries with backoff usually clear blips.
    for attempt in range(4):
        try:
            r = requests.get(full_zip_url, timeout=120)
            if not r.ok:
                return None
            z = zipfile.ZipFile(io.BytesIO(r.content))
            if "full.md" not in z.namelist():
                return None
            md = z.read("full.md").decode("utf-8")
            md_path.write_text(md, encoding="utf-8")
            return md_path
        except (requests.exceptions.RequestException, zipfile.BadZipFile) as e:
            if attempt < 3:
                wait = 2 ** attempt * 5  # 5, 10, 20s
                print(f"    [zip-dl retry {attempt + 1}] {type(e).__name__}: {e}; waiting {wait}s")
                time.sleep(wait)
            else:
                print(f"    [zip-dl GAVE UP] {file_path}: {e}")
                return None
    return None


def stage1_mineru(work: list[dict]) -> dict[str, str]:
    """{file_path: markdown}. Cache hits skip the API."""
    md_by_path: dict[str, str] = {}
    items_to_call: list[dict] = []
    missing_disk = 0
    unsupported_ext = 0

    for w in work:
        cache_path = MINERU_MD_DIR / f"{safe_cache_name(w['file_path'])}.md"
        if cache_path.exists():
            md_by_path[w["file_path"]] = cache_path.read_text(encoding="utf-8")
            continue
        disk = find_disk_path(w["filename"])
        if disk is None:
            missing_disk += 1
            continue
        if disk.suffix.lower() not in MINERU_SUPPORTED_EXTS:
            unsupported_ext += 1
            continue
        items_to_call.append({**w, "disk_path": disk})

    print(f"  cached={len(md_by_path)}  to_call={len(items_to_call)}  "
          f"missing_disk={missing_disk}  unsupported_ext={unsupported_ext}")
    if not items_to_call:
        return md_by_path

    batches = list(chunked(items_to_call, MINERU_BATCH_SIZE))
    for bi, batch in enumerate(batches, 1):
        print(f"\n  Batch {bi}/{len(batches)}: {len(batch)} files", flush=True)
        # Retry the whole batch up to 3 times for transient network errors.
        for attempt in range(3):
            try:
                batch_id, urls = mineru_request_batch(batch)
                print(f"    batch_id={batch_id}")
                mineru_upload_files(batch, urls)
                results = mineru_poll(batch_id)
                break
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                wait = 5 * (attempt + 1)
                print(f"    [transient] {type(e).__name__}, retry {attempt+1}/3 in {wait}s")
                time.sleep(wait)
                continue
            except Exception as e:
                print(f"    [BATCH FAIL] {e}")
                results = []
                break
        else:
            print(f"    [BATCH FAIL] gave up after 3 retries")
            continue
        if not results:
            continue

        for r in results:
            data_id = r.get("data_id")
            state = r.get("state")
            match = next((it for it in batch if safe_cache_name(it["file_path"])[:128] == data_id), None)
            if match is None:
                continue
            if state != "done":
                print(f"    [{match['filename']}] state={state}  err={r.get('err_msg')}")
                continue
            zip_url = r.get("full_zip_url")
            if zip_url is None:
                continue
            md_path = download_and_cache_md(match["file_path"], zip_url)
            if md_path:
                md_by_path[match["file_path"]] = md_path.read_text(encoding="utf-8")
                print(f"    [{match['filename']}] -> {md_path.stat().st_size} bytes")
    return md_by_path


# ── LLM extraction stage (single-property only) ──────────────────────────────

async def extract_description_from_md(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    markdown: str,
    prompt: str,
) -> str | None:
    full_prompt = (
        prompt
        + "\n\n---\nThe document text below was extracted by a layout-aware OCR "
          "tool (MinerU) and is provided as Markdown that preserves the "
          "original table structure. Read the Markdown to identify the "
          "property-description block.\n\n"
        + markdown
    )
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": full_prompt}],
        "max_tokens": 6144,
        "temperature": 0.1,
    }
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    async with sem:
        for attempt in range(MAX_RETRIES):
            try:
                async with session.post(
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status != 200:
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(2 ** attempt)
                        continue
                    data = await resp.json()
                    text = data["choices"][0]["message"]["content"].strip()
                    if text.startswith("```"):
                        lines = [l for l in text.split("\n") if not l.strip().startswith("```")]
                        text = "\n".join(lines).strip()
                    obj = None
                    try:
                        obj = json.loads(text)
                    except json.JSONDecodeError:
                        try:
                            s, e = text.find("{"), text.rfind("}") + 1
                            if s >= 0 and e > s:
                                obj = json.loads(text[s:e])
                        except json.JSONDecodeError:
                            obj = None
                    if not isinstance(obj, dict):
                        return None
                    val = obj.get("property_description_full")
                    return val if isinstance(val, str) and val.strip() else None
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
        return None


async def stage2_llm(work: list[dict], mds: dict[str, str]) -> dict[str, str | None]:
    """Run LLM extraction on single-property Documents only.
    Returns {file_path: description_or_None}."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY missing")
    NOTICE_DESC_V3_DIR.mkdir(parents=True, exist_ok=True)
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    # Filter to single-property Documents that have markdown and don't yet have a v3 cache.
    todo: list[tuple[str, str]] = []  # (file_path, markdown)
    cache_hits: dict[str, str] = {}
    for w in work:
        if w["notice_type"] != "single":
            continue
        fp = w["file_path"]
        md = mds.get(fp)
        if not md:
            continue
        cache_path = NOTICE_DESC_V3_DIR / f"{safe_cache_name(fp)}.json"
        if cache_path.exists():
            try:
                v = json.loads(cache_path.read_text(encoding="utf-8")).get("property_description_full")
            except Exception:
                v = None
            if isinstance(v, str) and v.strip():
                cache_hits[fp] = v
                continue
        todo.append((fp, md))

    print(f"  v3 cached: {len(cache_hits)}  to_extract: {len(todo)}")

    sem = asyncio.Semaphore(LLM_CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=LLM_CONCURRENCY * 2)
    new_results: dict[str, str | None] = {}

    async with aiohttp.ClientSession(connector=connector) as session:
        async def one(fp: str, md: str):
            try:
                desc = await extract_description_from_md(session, sem, md, prompt)
            except Exception as e:
                print(f"    [LLM-fail] {safe_cache_name(fp)[:60]}: {e}")
                desc = None
            new_results[fp] = desc
            cache_path = NOTICE_DESC_V3_DIR / f"{safe_cache_name(fp)}.json"
            try:
                cache_path.write_text(
                    json.dumps({"property_description_full": desc}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except Exception:
                pass

        # Process in chunks to keep memory bounded
        for chunk in chunked(todo, 50):
            await asyncio.gather(*[one(fp, md) for fp, md in chunk], return_exceptions=True)
            done = len({**cache_hits, **new_results})
            print(f"  [{done}/{len(cache_hits) + len(todo)}] cumulative")

    return {**cache_hits, **new_results}


# ── Apply stage ──────────────────────────────────────────────────────────────

def stage3_apply(work: list[dict], descs: dict[str, str | None]) -> int:
    """For each (single-property Document, listing) pair where the v3
    extraction succeeded, write description to Neo4j with source='notice'."""
    rows: list[dict] = []
    for w in work:
        if w["notice_type"] != "single":
            continue
        fp = w["file_path"]
        desc = descs.get(fp)
        if not desc:
            continue
        for aid in (w.get("aids") or []):
            if aid:
                rows.append({"auction_id": aid, "notice_description": desc})

    if not rows:
        print("  (nothing to apply)")
        return 0

    n = 0
    for batch in chunked(rows, WRITE_CHUNK):
        run_query("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.auction_id})
            SET a.description        = row.notice_description,
                a.description_source = 'notice'
        """, {"rows": batch})
        n += len(batch)
    return n


# ── Verification ─────────────────────────────────────────────────────────────

def print_summary():
    print("\n--- Final state ---")
    for row in run_read_query("""
        MATCH (a:AuctionProperty) RETURN a.description_source AS src, count(*) AS n ORDER BY src
    """):
        print(f"  source={str(row['src']):<20} {row['n']:>5}")
    md_count = len(list(MINERU_MD_DIR.glob("*.md"))) if MINERU_MD_DIR.exists() else 0
    v3_count = len(list(NOTICE_DESC_V3_DIR.glob("*.json"))) if NOTICE_DESC_V3_DIR.exists() else 0
    print(f"\n  mineru_markdown cache: {md_count} files")
    print(f"  notice_descriptions_v3 cache: {v3_count} files")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap to first N Documents (staged rollout)")
    parser.add_argument("--skip-mineru", action="store_true",
                        help="Skip MinerU stage; reuse cached markdowns only")
    parser.add_argument("--skip-apply", action="store_true",
                        help="Skip Neo4j writes (cache only)")
    args = parser.parse_args()

    if not MINERU_KEY:
        sys.exit("MINERU_API_KEY not set in .env")

    work = fetch_all_work()
    if args.limit:
        work = work[:args.limit]
    by_type = {}
    for w in work:
        by_type[w["notice_type"]] = by_type.get(w["notice_type"], 0) + 1
    print(f"Worklist: {len(work)} Documents  notice_type={by_type}")

    print("\n[Stage 1] MinerU OCR (markdown cache)")
    if args.skip_mineru:
        print("  --skip-mineru: loading cached markdowns only")
        mds: dict[str, str] = {}
        for w in work:
            cache_path = MINERU_MD_DIR / f"{safe_cache_name(w['file_path'])}.md"
            if cache_path.exists():
                mds[w["file_path"]] = cache_path.read_text(encoding="utf-8")
        print(f"  loaded {len(mds)} cached markdowns")
    else:
        mds = stage1_mineru(work)

    print(f"\n[Stage 2] LLM extraction (single-property only)")
    descs = asyncio.run(stage2_llm(work, mds))

    if args.skip_apply:
        print("\n[Stage 3] SKIPPED (--skip-apply)")
    else:
        print(f"\n[Stage 3] Apply v3 descriptions to Neo4j")
        n = stage3_apply(work, descs)
        print(f"  wrote {n} listings")

    print_summary()


if __name__ == "__main__":
    main()
