"""
pipeline/ocr_extract.py
-----------------------
Stage 1: Vision LLM OCR + Entity Extraction (PRD 5.5 + 5.6 merged).

For each TN auction record, sends its associated image(s) to a vision LLM
via OpenRouter to extract structured entities. Results are cached per-file
and merged per-record into output/extracted.jsonl.

Run standalone:  python -m pipeline.ocr_extract
"""

import asyncio
import aiohttp
import base64
import json
import time
from pathlib import Path

from pipeline.config import (
    INPUT_JSONL, DOWNLOADS_DIR, CACHE_DIR, OUTPUT_DIR,
    PROMPTS_DIR, OPENROUTER_API_KEY, OPENROUTER_BASE_URL,
    OPENROUTER_MODEL, BATCH_SIZE, MAX_RETRIES, RATE_LIMIT_DELAY,
)


# ── Load prompt template ─────────────────────────────────────────────────────
PROMPT_TEMPLATE = (PROMPTS_DIR / "extract_auction.txt").read_text(encoding="utf-8")

# ── Output path ──────────────────────────────────────────────────────────────
EXTRACTED_JSONL = OUTPUT_DIR / "extracted.jsonl"

# Supported image extensions (sent directly to vision API)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".jfif"}
# PDF needs conversion to image first
PDF_EXTS = {".pdf"}


def load_records() -> list[dict]:
    """Load all records from the TN auction JSONL."""
    records = []
    with open(INPUT_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def get_cache_path(auction_id: str, filename: str) -> Path:
    """Return cache file path for a specific file extraction."""
    safe_name = filename.replace("/", "_").replace("\\", "_")
    return CACHE_DIR / f"{auction_id}__{safe_name}.json"


def read_cache(auction_id: str, filename: str) -> dict | None:
    """Read cached extraction result if it exists."""
    path = get_cache_path(auction_id, filename)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def write_cache(auction_id: str, filename: str, result: dict):
    """Write extraction result to cache."""
    path = get_cache_path(auction_id, filename)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def encode_image_to_base64(file_path: Path) -> str | None:
    """Read and base64-encode an image file."""
    try:
        data = file_path.read_bytes()
        return base64.b64encode(data).decode("utf-8")
    except OSError:
        return None


def get_mime_type(ext: str) -> str:
    """Map file extension to MIME type."""
    mapping = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".jfif": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".avif": "image/avif",
    }
    return mapping.get(ext.lower(), "image/jpeg")


def pdf_to_images(pdf_path: Path) -> list[tuple[str, str]]:
    """Convert PDF pages to base64-encoded images. Returns list of (base64_str, mime_type)."""
    try:
        from pdf2image import convert_from_path
        images = convert_from_path(str(pdf_path), dpi=200, first_page=1, last_page=5)
        results = []
        for img in images:
            import io
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            results.append((b64, "image/jpeg"))
        return results
    except ImportError:
        print("  [WARN] pdf2image not installed, skipping PDF")
        return []
    except Exception as e:
        print(f"  [WARN] PDF conversion failed for {pdf_path}: {e}")
        return []


def build_prompt(website_description: str) -> str:
    """Fill in the prompt template with the website description."""
    return PROMPT_TEMPLATE.replace("{website_description}", website_description or "(No description available)")


def parse_llm_response(text: str) -> dict | None:
    """Parse JSON from LLM response, handling markdown code blocks."""
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (```json and ```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                return None
    return None


async def call_vision_api(
    session: aiohttp.ClientSession,
    b64_images: list[tuple[str, str]],  # (base64_data, mime_type)
    prompt: str,
    semaphore: asyncio.Semaphore,
) -> dict | None:
    """Send image(s) to OpenRouter vision API and return parsed result."""
    async with semaphore:
        # Build content array with text prompt + images
        content = [{"type": "text", "text": prompt}]
        for b64_data, mime_type in b64_images:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{b64_data}"
                }
            })

        payload = {
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": content}],
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
                    text = data["choices"][0]["message"]["content"]
                    return parse_llm_response(text)

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                print(f"  [NET ERROR] Attempt {attempt + 1}: {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)

        return None


def cross_reference(record: dict, extracted: dict) -> dict:
    """Compare extracted data with website data, flag what's new."""
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

    # Check description completeness
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
) -> dict | None:
    """Process a single auction record: extract data from its associated files."""
    auction_id = record.get("auction_id", "")
    downloads = record.get("downloads_found") or []

    if not downloads:
        return None

    prompt = build_prompt(record.get("description", ""))
    merged_extraction = {}

    for filename in downloads:
        # Check cache first
        cached = read_cache(auction_id, filename)
        if cached:
            merged_extraction = {**merged_extraction, **cached}
            continue

        file_path = DOWNLOADS_DIR / filename
        if not file_path.exists():
            # Try case-insensitive search
            matches = list(DOWNLOADS_DIR.glob(f"*{filename}*"))
            if matches:
                file_path = matches[0]
            else:
                continue

        ext = file_path.suffix.lower()

        # Prepare images for the API
        b64_images = []
        if ext in IMAGE_EXTS:
            b64 = encode_image_to_base64(file_path)
            if b64:
                b64_images.append((b64, get_mime_type(ext)))
        elif ext in PDF_EXTS:
            b64_images = pdf_to_images(file_path)
        else:
            continue

        if not b64_images:
            continue

        # Call vision API
        result = await call_vision_api(session, b64_images, prompt, semaphore)

        if result:
            write_cache(auction_id, filename, result)
            merged_extraction = {**merged_extraction, **{k: v for k, v in result.items() if v is not None}}

        await asyncio.sleep(RATE_LIMIT_DELAY)

    if not merged_extraction:
        return None

    # Cross-reference with website data
    xref = cross_reference(record, merged_extraction)

    return {
        "auction_id": auction_id,
        "url": record.get("url"),
        "extracted": merged_extraction,
        "cross_reference": xref,
    }


async def run_extraction(limit: int | None = None):
    """Run OCR + entity extraction on all TN records."""
    records = load_records()
    if limit:
        records = records[:limit]

    total = len(records)
    print(f"OCR Extraction: {total} records to process")
    print(f"  Model: {OPENROUTER_MODEL}")
    print(f"  Concurrency: {BATCH_SIZE}")

    # Load already-processed auction IDs from output file
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

    # Filter out already-processed records
    pending = [r for r in records if r.get("auction_id") not in processed_ids]
    print(f"  Pending: {len(pending)}")

    if not pending:
        print("  Nothing to do.")
        return

    semaphore = asyncio.Semaphore(BATCH_SIZE)
    completed = 0
    errors = 0
    t_start = time.time()

    connector = aiohttp.TCPConnector(limit=BATCH_SIZE * 2)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Process in chunks for progress reporting
        chunk_size = BATCH_SIZE * 5
        with open(EXTRACTED_JSONL, "a", encoding="utf-8") as out_f:
            for i in range(0, len(pending), chunk_size):
                chunk = pending[i : i + chunk_size]
                tasks = [process_record(r, session, semaphore) for r in chunk]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for result in results:
                    if isinstance(result, Exception):
                        errors += 1
                        print(f"\n  [ERROR] {result}")
                    elif result is not None:
                        out_f.write(json.dumps(result, ensure_ascii=False) + "\n")
                        completed += 1
                    else:
                        completed += 1  # No downloads to process

                out_f.flush()
                elapsed = time.time() - t_start
                total_done = len(processed_ids) + completed + errors
                rate = (completed + errors) / elapsed if elapsed > 0 else 0
                print(f"  [{total_done}/{total}] {rate:.1f} rec/s | {completed} extracted | {errors} errors", end="\r")

    elapsed = time.time() - t_start
    print(f"\n\n  Completed: {completed} | Errors: {errors} | Time: {elapsed:.1f}s")
    print(f"  Output: {EXTRACTED_JSONL}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="OCR + Entity Extraction Pipeline")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of records to process")
    args = parser.parse_args()
    asyncio.run(run_extraction(limit=args.limit))


if __name__ == "__main__":
    main()
