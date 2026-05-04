"""Multi-property notice splitter — extraction stage.

Reads :Document.markdown (or pipeline/cache/mineru_markdown/<safe>.md) for
every multi-property notice (notice_type='multi'), runs an LLM that returns
an array of `{reserve_price_num, property_description_full}` (one per
auction lot in the notice), and caches the result for the apply stage.

The model is hard-coded to `deepseek/deepseek-v4-pro` so this script does
NOT touch the v3 single-property pipeline's `OPENROUTER_MODEL` setting.

Cache:
  pipeline/cache/notice_descriptions_v3_multi/<safe_path>.json — one file
  per multi-Document with shape:
    {"schedules": [{"reserve_price_num": int|null,
                    "property_description_full": "..."}]}

Resumable: re-runs skip Documents whose cache file already exists. Pre-flight:
the script makes one test LLM call before kicking off the full run; if the
model id is wrong it aborts with a clear error so the user can correct it.

Usage:
  python -m scripts.extract_multi_descriptions             # full run
  python -m scripts.extract_multi_descriptions --limit 5   # cap to first 5 multis (smoke)
  python -m scripts.extract_multi_descriptions --skip-preflight   # skip the 1-call gate
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

from api.neo4j_client import run_read_query
from pipeline.config import (
    OPENROUTER_API_KEY, OPENROUTER_BASE_URL,
    PROMPTS_DIR, MAX_RETRIES,
)


load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
MULTI_CACHE_DIR = REPO_ROOT / "pipeline" / "cache" / "notice_descriptions_v3_multi"
MD_DIR = REPO_ROOT / "pipeline" / "cache" / "mineru_markdown"
PROMPT_PATH = PROMPTS_DIR / "extract_description_multi.txt"

# Hard-coded for this script — keeps the v3 single pipeline's OPENROUTER_MODEL
# untouched. To change the multi splitter's model, edit this constant.
#
# v4-pro is a reasoning model: most of the token budget goes into chain-of-
# thought, often leaving content=null on long inputs. v4-flash is the
# non-reasoning sibling — clean content, faster (22s vs 55s for the
# reasoning models), faithful to the prompt's "do NOT summarize" rule.
# Validated on a 3-property ICICI notice: returned 3 schedules with
# correct reserve prices and full per-property descriptions.
LLM_MODEL = "deepseek/deepseek-v4-flash"

LLM_CONCURRENCY = 6
CHUNK_SIZE = 50


def safe_cache_name(file_path: str) -> str:
    return file_path.replace("/", "_").replace("\\", "_").replace(":", "_")


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_multi_work() -> list[dict]:
    """Every multi-property Document with markdown available."""
    return run_read_query("""
      MATCH (d:Document)
      WHERE d.notice_type = 'multi'
        AND d.markdown IS NOT NULL
        AND d.markdown <> ''
      RETURN d.file_path     AS file_path,
             d.markdown      AS markdown,
             d.property_count AS property_count
    """, max_rows=10_000)


def parse_llm_json(text: str) -> dict | None:
    """Strip code-fences and parse a JSON object out of text. Returns the
    parsed dict, or None on parse failure."""
    text = text.strip()
    if text.startswith("```"):
        lines = [l for l in text.split("\n") if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        try:
            s, e = text.find("{"), text.rfind("}") + 1
            if s >= 0 and e > s:
                obj = json.loads(text[s:e])
            else:
                return None
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


def normalize_schedules(obj: dict) -> list[dict] | None:
    """Validate and clean the LLM output. Returns a list of
    {reserve_price_num, property_description_full} dicts, or None on
    structural failure."""
    schedules = obj.get("schedules")
    if not isinstance(schedules, list) or not schedules:
        return None
    cleaned: list[dict] = []
    for s in schedules:
        if not isinstance(s, dict):
            continue
        desc = s.get("property_description_full")
        if not isinstance(desc, str) or not desc.strip():
            continue
        rp = s.get("reserve_price_num")
        # Coerce string prices to int when possible (LLMs sometimes return
        # "17320325" as a string even when asked for int)
        if isinstance(rp, str):
            try:
                rp = int(rp.replace(",", "").replace(" ", ""))
            except ValueError:
                rp = None
        if not isinstance(rp, int):
            rp = None
        cleaned.append({"reserve_price_num": rp,
                        "property_description_full": desc.strip()})
    return cleaned or None


async def call_llm(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    markdown: str,
    prompt: str,
) -> dict | None:
    """One LLM call. Returns parsed dict {schedules: [...]} or None on
    permanent failure."""
    full_prompt = (
        prompt
        + "\n\n---\nThe document text below was extracted by a layout-aware "
          "OCR tool (MinerU) and is provided as Markdown that preserves the "
          "original table structure. Read the Markdown to identify each "
          "auction lot.\n\n"
        + markdown
    )
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": full_prompt}],
        # Multi-property output scales with property_count. The 9-property
        # ARCIL notice can produce ~30K tokens of verbatim text. Sized well
        # above p95 to avoid truncation; deepseek-v4-pro handles it.
        "max_tokens": 32768,
        "temperature": 0.1,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    async with sem:
        for attempt in range(MAX_RETRIES):
            try:
                async with session.post(
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=180),
                ) as resp:
                    if resp.status == 404:
                        body = await resp.text()
                        raise RuntimeError(
                            f"OpenRouter 404 for model '{LLM_MODEL}'. "
                            f"The model id is wrong or unavailable. "
                            f"Body: {body[:300]}"
                        )
                    # 403 = key limit exceeded / auth error. Don't retry —
                    # the key needs human intervention. Raise loud so the
                    # whole run aborts instead of silently failing every call.
                    if resp.status == 403:
                        body = await resp.text()
                        raise RuntimeError(
                            f"OpenRouter 403 (auth/key limit). The API key "
                            f"has hit a spending cap or is invalid. "
                            f"Manage at https://openrouter.ai/settings/keys . "
                            f"Body: {body[:300]}"
                        )
                    if resp.status != 200:
                        body = await resp.text()
                        # Surface the error so it's debuggable instead of
                        # being silently retried-then-given-up.
                        if attempt == MAX_RETRIES - 1:
                            print(f"    [HTTP {resp.status}] {body[:200]}")
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(2 ** attempt)
                        continue
                    data = await resp.json()
                    text = data["choices"][0]["message"]["content"]
                    return parse_llm_json(text)
            except RuntimeError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == MAX_RETRIES - 1:
                    print(f"    [network] {type(e).__name__}: {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
        return None


async def preflight(prompt: str) -> None:
    """One LLM call against any cached multi-notice to verify the model id
    works. Aborts with a clear error if 404 / unavailable. Picks a 2-3
    property notice in the 2-8KB range so we don't burn tokens on the
    ARCIL/9-property giant or stumble over an image-only degenerate."""
    work = fetch_multi_work()
    if not work:
        print("Pre-flight: no multi-notices in worklist; skipping.")
        return
    candidates = [
        w for w in work
        if w.get("property_count") in (2, 3)
        and 2000 <= len(w["markdown"]) <= 8000
    ]
    sample = candidates[0] if candidates else work[0]
    print(f"Pre-flight: testing {LLM_MODEL} on '{sample['file_path']}' "
          f"(pc={sample.get('property_count')}, md={len(sample['markdown'])} chars)...")
    sem = asyncio.Semaphore(1)
    async with aiohttp.ClientSession() as session:
        try:
            obj = await call_llm(session, sem, sample["markdown"], prompt)
        except RuntimeError as e:
            print(f"\n  PRE-FLIGHT FAILED: {e}")
            print(f"\n  Fix the model id by editing scripts/extract_multi_descriptions.py "
                  f"(constant `LLM_MODEL`). Suggested fallbacks:")
            print(f"    deepseek/deepseek-v3.2")
            print(f"    google/gemini-2.5-pro")
            print(f"    google/gemini-2.5-flash")
            sys.exit(1)
    if obj is None:
        print("  Pre-flight: LLM returned no parseable JSON; aborting.")
        sys.exit(1)
    schedules = normalize_schedules(obj)
    if not schedules:
        print("  Pre-flight: LLM returned no usable schedules; aborting.")
        sys.exit(1)
    print(f"  OK: {LLM_MODEL} returned {len(schedules)} schedule(s) for "
          f"property_count={sample['property_count']}.")


async def extract_all(work: list[dict], prompt: str) -> dict:
    """Run extraction on the worklist. Cache hits skip the LLM. Returns a
    summary dict."""
    MULTI_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    todo: list[dict] = []
    cache_hits = 0
    for w in work:
        cache_path = MULTI_CACHE_DIR / f"{safe_cache_name(w['file_path'])}.json"
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if cached.get("schedules"):
                    cache_hits += 1
                    continue
            except Exception:
                pass
        todo.append(w)

    print(f"Cached: {cache_hits}  to_extract: {len(todo)}")
    if not todo:
        return {"cached": cache_hits, "extracted": 0, "failed": 0,
                "count_mismatches": 0}

    sem = asyncio.Semaphore(LLM_CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=LLM_CONCURRENCY * 2)
    extracted = 0
    failed = 0
    count_mismatches = 0

    async with aiohttp.ClientSession(connector=connector) as session:
        # Shared abort flag — once any worker raises a fatal error
        # (e.g. 403 key-limit), every other inflight worker bails out
        # early instead of burning calls that will all fail the same way.
        fatal_error: list[BaseException] = []

        async def one(w: dict):
            nonlocal extracted, failed, count_mismatches
            if fatal_error:
                return
            fp = w["file_path"]
            try:
                obj = await call_llm(session, sem, w["markdown"], prompt)
            except RuntimeError as e:
                # 403 / 404 / model-id errors are fatal for the whole run.
                if not fatal_error:
                    fatal_error.append(e)
                    print(f"\n  FATAL: {e}\n  Aborting remaining work.")
                return
            except Exception as e:
                print(f"  [LLM-fail] {safe_cache_name(fp)[:60]}: {e}")
                failed += 1
                return
            if obj is None:
                failed += 1
                return
            schedules = normalize_schedules(obj)
            if not schedules:
                failed += 1
                return
            if w["property_count"] is not None and len(schedules) != w["property_count"]:
                count_mismatches += 1
                print(f"  [count-mismatch] {safe_cache_name(fp)[:60]}: "
                      f"LLM returned {len(schedules)} schedules, "
                      f"property_count={w['property_count']}")
                # Don't drop the result — apply stage will best-effort match
                # by reserve_price_num and log unmatched listings.
            cache_path = MULTI_CACHE_DIR / f"{safe_cache_name(fp)}.json"
            cache_path.write_text(
                json.dumps({"schedules": schedules}, ensure_ascii=False, indent=2),
                encoding="utf-8")
            extracted += 1

        for chunk in chunked(todo, CHUNK_SIZE):
            if fatal_error:
                break
            await asyncio.gather(*[one(w) for w in chunk], return_exceptions=True)
            done = cache_hits + extracted + failed
            total = cache_hits + len(todo)
            print(f"  [{done}/{total}]  extracted={extracted}  "
                  f"failed={failed}  mismatches={count_mismatches}")
            if fatal_error:
                break

        if fatal_error:
            raise fatal_error[0]

    return {"cached": cache_hits, "extracted": extracted, "failed": failed,
            "count_mismatches": count_mismatches}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N multi-notices (staged rollout)")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="skip the 1-call model-id sanity check")
    args = ap.parse_args()

    if not OPENROUTER_API_KEY:
        print("OPENROUTER_API_KEY not set in .env")
        return 1

    if not PROMPT_PATH.exists():
        print(f"Prompt missing: {PROMPT_PATH}")
        return 1
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    print(f"Multi-property splitter")
    print(f"  Model:    {LLM_MODEL}")
    print(f"  Cache:    {MULTI_CACHE_DIR}")
    print(f"  Prompt:   {PROMPT_PATH}")
    print()

    if not args.skip_preflight:
        asyncio.run(preflight(prompt))
        print()

    work = fetch_multi_work()
    if args.limit:
        work = work[:args.limit]
    print(f"Worklist: {len(work)} multi-property Documents with markdown")

    summary = asyncio.run(extract_all(work, prompt))
    print()
    print(f"Done. cached={summary['cached']}  extracted={summary['extracted']}  "
          f"failed={summary['failed']}  count_mismatches={summary['count_mismatches']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
