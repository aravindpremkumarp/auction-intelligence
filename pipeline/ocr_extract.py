"""
pipeline/ocr_extract.py
-----------------------
Stage 1: Structured entity extraction from auction notice documents (PRD 5.5 + 5.6 merged).

For each TN auction record, look up the layout-aware MinerU markdown for each
associated document and send it to a text LLM (via OpenRouter) to extract
structured entities. Results are cached per-file and merged per-record into
output/extracted.jsonl.

Markdown comes from ``pipeline/cache/mineru_markdown/`` — populated by
``scripts/ocr_with_mineru.py``. Files without cached markdown are skipped here;
re-run the MinerU script first to widen coverage.

Run standalone:  python -m pipeline.ocr_extract
"""

import asyncio
import aiohttp
import json
import time
from pathlib import Path

from pipeline.obs import USAGE, get_logger
from pipeline.config import (
    INPUT_JSONL, CACHE_DIR, OUTPUT_DIR,
    PROMPTS_DIR, OPENROUTER_API_KEY, OPENROUTER_BASE_URL,
    OPENROUTER_MODEL, BATCH_SIZE, MAX_RETRIES, RATE_LIMIT_DELAY,
)
from pipeline.mineru import cached_markdown_for_filename


PROMPT_TEMPLATE = (PROMPTS_DIR / "extract_auction.txt").read_text(encoding="utf-8")

EXTRACTED_JSONL = OUTPUT_DIR / "extracted.jsonl"

log = get_logger(__name__)


def load_records() -> list[dict]:
    records = []
    skipped = 0
    with open(INPUT_JSONL, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    skipped += 1
                    log.warning("load_records skipped malformed line %d: %.100s", i, line)
    if skipped:
        log.warning("load_records skipped=%d of input lines", skipped)
    return records


def get_cache_path(auction_id: str, filename: str) -> Path:
    safe_name = filename.replace("/", "_").replace("\\", "_")
    return CACHE_DIR / f"{auction_id}__{safe_name}.json"


def read_cache(auction_id: str, filename: str) -> dict | None:
    path = get_cache_path(auction_id, filename)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("read_cache corrupt/unreadable %s: %s", path.name, e)
            return None
    return None


def write_cache(auction_id: str, filename: str, result: dict):
    path = get_cache_path(auction_id, filename)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def build_prompt(website_description: str, markdown: str) -> str:
    body = PROMPT_TEMPLATE.replace(
        "{website_description}",
        website_description or "(No description available)",
    )
    return f"{body}\n\n--- BEGIN OCR MARKDOWN ---\n{markdown}\n--- END OCR MARKDOWN ---\n"


def parse_llm_response(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                return None
    return None


async def call_text_llm(
    session: aiohttp.ClientSession,
    prompt: str,
    semaphore: asyncio.Semaphore,
) -> dict | None:
    """Send a text-only prompt to OpenRouter and return parsed JSON."""
    async with semaphore:
        payload = {
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4096,
            "temperature": 0.1,
        }
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://bank-auction-intelligence.local",
        }

        for attempt in range(MAX_RETRIES):
            try:
                async with session.post(
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", 5))
                        print(f"  [RATE LIMITED] Waiting {retry_after}s...")
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status != 200:
                        body = await resp.text()
                        print(f"  [API ERROR] {resp.status}: {body[:200]}")
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(2 ** attempt)
                        continue

                    data = await resp.json()
                    USAGE.record(data.get("usage"))
                    text = data["choices"][0]["message"]["content"]
                    return parse_llm_response(text)

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                print(f"  [NET ERROR] Attempt {attempt + 1}: {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)

        return None


def cross_reference(record: dict, extracted: dict) -> dict:
    new_info = []
    website_desc = (record.get("description") or "").lower()

    if extracted.get("undivided_share"):
        new_info.append("undivided_share")

    if extracted.get("village") and extracted["village"].lower() not in website_desc:
        new_info.append("village")

    if extracted.get("taluk") and extracted["taluk"].lower() not in website_desc:
        new_info.append("taluk")

    if extracted.get("boundaries") and any(extracted["boundaries"].values()):
        new_info.append("boundaries")

    img_desc = (extracted.get("property_description_full") or "")
    web_len = len(record.get("description") or "")
    img_len = len(img_desc)
    completeness = min(web_len / max(img_len, 1), 1.0) if img_len > 0 else 1.0

    return {
        "new_info_found": new_info,
        "description_completeness": round(completeness, 2),
    }


async def process_record(
    record: dict,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    stats: dict,
) -> dict | None:
    auction_id = record.get("auction_id", "")
    downloads = record.get("downloads_found") or []

    if not downloads:
        return None

    merged_extraction = {}

    for filename in downloads:
        cached = read_cache(auction_id, filename)
        if cached:
            merged_extraction = {**merged_extraction, **cached}
            continue

        markdown = cached_markdown_for_filename(filename)
        if not markdown:
            stats["missing_markdown"] = stats.get("missing_markdown", 0) + 1
            continue

        prompt = build_prompt(record.get("description", ""), markdown)
        result = await call_text_llm(session, prompt, semaphore)

        if result:
            write_cache(auction_id, filename, result)
            merged_extraction = {**merged_extraction, **{k: v for k, v in result.items() if v is not None}}

        await asyncio.sleep(RATE_LIMIT_DELAY)

    if not merged_extraction:
        return None

    xref = cross_reference(record, merged_extraction)

    return {
        "auction_id": auction_id,
        "url": record.get("url"),
        "extracted": merged_extraction,
        "cross_reference": xref,
    }


async def run_extraction(limit: int | None = None):
    records = load_records()
    if limit:
        records = records[:limit]

    total = len(records)
    print(f"OCR Extraction: {total} records to process")
    print(f"  Model: {OPENROUTER_MODEL}")
    print(f"  Concurrency: {BATCH_SIZE}")
    print(f"  Input: cached MinerU markdown only (text LLM)")

    processed_ids = set()
    if EXTRACTED_JSONL.exists():
        with open(EXTRACTED_JSONL, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        obj = json.loads(line)
                        processed_ids.add(obj.get("auction_id"))
                    except json.JSONDecodeError:
                        pass
    print(f"  Already processed: {len(processed_ids)}")

    pending = [r for r in records if r.get("auction_id") not in processed_ids]
    print(f"  Pending: {len(pending)}")

    if not pending:
        print("  Nothing to do.")
        return

    semaphore = asyncio.Semaphore(BATCH_SIZE)
    completed = 0
    errors = 0
    stats: dict = {}
    t_start = time.time()

    connector = aiohttp.TCPConnector(limit=BATCH_SIZE * 2)
    async with aiohttp.ClientSession(connector=connector) as session:
        chunk_size = BATCH_SIZE * 5
        with open(EXTRACTED_JSONL, "a", encoding="utf-8") as out_f:
            for i in range(0, len(pending), chunk_size):
                chunk = pending[i : i + chunk_size]
                tasks = [process_record(r, session, semaphore, stats) for r in chunk]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for result in results:
                    if isinstance(result, Exception):
                        errors += 1
                        print(f"\n  [ERROR] {result}")
                    elif result is not None:
                        out_f.write(json.dumps(result, ensure_ascii=False) + "\n")
                        completed += 1
                    else:
                        completed += 1

                out_f.flush()
                elapsed = time.time() - t_start
                total_done = len(processed_ids) + completed + errors
                rate = (completed + errors) / elapsed if elapsed > 0 else 0
                print(f"  [{total_done}/{total}] {rate:.1f} rec/s | {completed} extracted | {errors} errors", end="\r")

    elapsed = time.time() - t_start
    print(f"\n\n  Completed: {completed} | Errors: {errors} | Time: {elapsed:.1f}s")
    log.info(USAGE.summary("ocr_extract"))
    if stats.get("missing_markdown"):
        print(f"  [WARN] {stats['missing_markdown']} document(s) had no cached MinerU markdown and were skipped.")
        print(f"         Run `python -m scripts.ocr_with_mineru --skip-apply` first to populate the cache.")
    print(f"  Output: {EXTRACTED_JSONL}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="OCR + Entity Extraction Pipeline")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of records to process")
    args = parser.parse_args()
    asyncio.run(run_extraction(limit=args.limit))


if __name__ == "__main__":
    main()
