"""Per-Document description extraction orchestrator.

Picks the right prompt + model based on Document.notice_type:
  notice_type='single' -> extract_description.txt, OPENROUTER_MODEL_DESCRIPTION_SINGLE
                          cache: pipeline/cache/notice_descriptions_v3/<safe>.json
  notice_type='multi'  -> extract_description_multi.txt, OPENROUTER_MODEL_DESCRIPTION_MULTI
                          cache: pipeline/cache/notice_descriptions_v3_multi/<safe>.json

Cache semantics. Each Document carries
``Document.description_extraction_status`` ∈ {pending, cached, applied,
failed, count_mismatch, needs_reextract}. The pipeline writes that status
after every run; the review API writes 'needs_reextract' when a reviewer
flips the classification so the next pipeline run regenerates the cache
file (overwriting the stale extraction). Status 'applied' means the cache
is good and the descriptions are already in Neo4j — re-runs skip.

Run:  python -m pipeline.extract_descriptions
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

from api.neo4j_client import run_query, run_read_query
from pipeline.obs import USAGE, get_logger
from pipeline.config import (
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL_DESCRIPTION_SINGLE,
    OPENROUTER_MODEL_DESCRIPTION_MULTI,
    NOTICE_DESC_SINGLE_DIR,
    NOTICE_DESC_MULTI_DIR,
    PROMPTS_DIR,
    MAX_RETRIES,
    DESC_LLM_CONCURRENCY,
)

log = get_logger(__name__)


load_dotenv()

SINGLE_PROMPT_PATH = PROMPTS_DIR / "extract_description.txt"
MULTI_PROMPT_PATH  = PROMPTS_DIR / "extract_description_multi.txt"

LLM_CONCURRENCY = DESC_LLM_CONCURRENCY   # env-tunable (DESC_LLM_CONCURRENCY)
CHUNK_SIZE = 50


def safe_cache_name(file_path: str) -> str:
    return file_path.replace("/", "_").replace("\\", "_").replace(":", "_")


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def parse_llm_json(text: str) -> dict | None:
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


def fetch_extraction_work(missing_only: bool = False) -> list[dict]:
    """Every Document that needs (or might need) a description extraction.

    The orchestrator decides what to do per row using cache + status.

    When ``missing_only`` is True, the worklist is restricted to Documents
    that back at least one :AuctionProperty whose description was NOT sourced
    from a notice (description_source not in {'notice','human'}). This is the
    safe way to backfill properties that were never extracted without
    re-extracting — and potentially overwriting — the descriptions already
    applied to other listings.
    """
    missing_clause = ""
    if missing_only:
        missing_clause = """
        AND EXISTS {
          MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d)
          WHERE NOT coalesce(a.description_source, '') IN ['notice', 'human']
        }"""
    return run_read_query(f"""
      MATCH (d:Document)
      WHERE d.markdown IS NOT NULL
        AND d.markdown <> ''
        AND d.notice_type IN ['single', 'multi']{missing_clause}
      RETURN d.file_path                       AS file_path,
             d.filename                        AS filename,
             d.markdown                        AS markdown,
             d.notice_type                     AS notice_type,
             coalesce(d.property_count, 0)     AS property_count,
             coalesce(d.description_extraction_status, 'pending')
                                               AS status
    """, max_rows=20_000, timeout=30.0)


async def call_extraction_llm(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    markdown: str,
    prompt: str,
    model: str,
    max_tokens: int,
) -> dict | None:
    full = (
        prompt
        + "\n\n---\nThe document text below was extracted by a layout-aware "
          "OCR tool (MinerU) and is provided as Markdown that preserves the "
          "original table structure.\n\n"
        + markdown
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": full}],
        "max_tokens": max_tokens,
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
                            f"OpenRouter 404 for model '{model}'. Body: {body[:200]}"
                        )
                    if resp.status == 403:
                        body = await resp.text()
                        raise RuntimeError(
                            f"OpenRouter 403 (auth/key limit) for '{model}'. "
                            f"Body: {body[:200]}"
                        )
                    if resp.status != 200:
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(2 ** attempt)
                        continue
                    data = await resp.json()
                    USAGE.record(data.get("usage"))
                    text = data["choices"][0]["message"]["content"]
                    return parse_llm_json(text)
            except RuntimeError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
        return None


def cache_path_for(notice_type: str, file_path: str) -> Path:
    base = NOTICE_DESC_SINGLE_DIR if notice_type == "single" else NOTICE_DESC_MULTI_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{safe_cache_name(file_path)}.json"


def existing_cache_payload(notice_type: str, file_path: str) -> dict | None:
    """Read the on-disk cache for a Document. Returns None when missing
    or unparseable."""
    p = cache_path_for(notice_type, file_path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("corrupt extraction cache %s: %s", p.name, e)
        return None


def write_extraction_status(rows: list[dict]) -> None:
    """Stamp description_extraction_status + description_extracted_at."""
    if not rows:
        return
    for batch in chunked(rows, 200):
        run_query("""
            UNWIND $rows AS row
            MATCH (d:Document {file_path: row.file_path})
            SET d.description_extraction_status = row.status,
                d.description_extracted_at      = datetime(row.at)
        """, {"rows": batch})


async def run_async(
    work: list[dict],
    force: bool = False,
) -> dict:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY missing")
    if not SINGLE_PROMPT_PATH.exists() or not MULTI_PROMPT_PATH.exists():
        raise RuntimeError("missing extraction prompt files in pipeline/prompts/")

    single_prompt = SINGLE_PROMPT_PATH.read_text(encoding="utf-8")
    multi_prompt  = MULTI_PROMPT_PATH.read_text(encoding="utf-8")

    # Decide cache vs new extraction
    todo: list[dict] = []
    cache_hits = 0
    status_rows: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for w in work:
        nt = w["notice_type"]
        fp = w["file_path"]
        status = w.get("status") or "pending"

        # Hard skip — already applied and not flagged for re-extraction
        if status == "applied" and not force:
            continue

        cached = existing_cache_payload(nt, fp)
        cache_ok = False
        if cached and not force and status != "needs_reextract":
            if nt == "single":
                desc = cached.get("property_description_full")
                cache_ok = isinstance(desc, str) and bool(desc.strip())
            else:
                cache_ok = bool(normalize_schedules(cached) if cached else None)
        if cache_ok:
            cache_hits += 1
            status_rows.append({"file_path": fp, "status": "cached", "at": now_iso})
            continue
        todo.append(w)

    print(f"  cache_hits={cache_hits}  to_extract={len(todo)}")

    extracted_single = 0
    extracted_multi  = 0
    failed = 0
    count_mismatches = 0

    if todo:
        sem = asyncio.Semaphore(LLM_CONCURRENCY)
        connector = aiohttp.TCPConnector(limit=LLM_CONCURRENCY * 2)
        async with aiohttp.ClientSession(connector=connector) as session:
            fatal: list[BaseException] = []

            async def one(w: dict):
                nonlocal extracted_single, extracted_multi, failed, count_mismatches
                if fatal:
                    return
                fp = w["file_path"]
                nt = w["notice_type"]
                if nt == "single":
                    prompt = single_prompt
                    model  = OPENROUTER_MODEL_DESCRIPTION_SINGLE
                    max_tok = 6144
                else:
                    prompt = multi_prompt
                    model  = OPENROUTER_MODEL_DESCRIPTION_MULTI
                    max_tok = 32768
                try:
                    obj = await call_extraction_llm(session, sem, w["markdown"],
                                                     prompt, model, max_tok)
                except RuntimeError as e:
                    if not fatal:
                        fatal.append(e)
                        print(f"\n  FATAL: {e}")
                    return
                except Exception as e:
                    failed += 1
                    status_rows.append({"file_path": fp, "status": "failed",
                                        "at": now_iso})
                    print(f"  [LLM-fail] {safe_cache_name(fp)[:60]}: {e}")
                    return
                if obj is None:
                    failed += 1
                    status_rows.append({"file_path": fp, "status": "failed",
                                        "at": now_iso})
                    return

                cache_path = cache_path_for(nt, fp)
                if nt == "single":
                    desc = obj.get("property_description_full")
                    if not isinstance(desc, str) or not desc.strip():
                        failed += 1
                        status_rows.append({"file_path": fp, "status": "failed",
                                            "at": now_iso})
                        return
                    try:
                        cache_path.write_text(json.dumps({
                            "property_description_full": desc.strip(),
                        }, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception as e:
                        # A lost cache write must not be stamped "cached" in the
                        # graph — apply_descriptions would then read a missing file.
                        failed += 1
                        log.error("cache write failed %s: %s", cache_path.name, e)
                        status_rows.append({"file_path": fp, "status": "failed",
                                            "at": now_iso})
                        return
                    extracted_single += 1
                    status_rows.append({"file_path": fp, "status": "cached",
                                        "at": now_iso})
                    return

                # multi
                schedules = normalize_schedules(obj)
                if not schedules:
                    failed += 1
                    status_rows.append({"file_path": fp, "status": "failed",
                                        "at": now_iso})
                    return
                pc = int(w.get("property_count") or 0)
                this_status = "cached"
                if pc and len(schedules) != pc:
                    count_mismatches += 1
                    this_status = "count_mismatch"
                    print(f"  [count-mismatch] {safe_cache_name(fp)[:60]}: "
                          f"LLM returned {len(schedules)}, property_count={pc}")
                try:
                    cache_path.write_text(json.dumps({"schedules": schedules},
                                                       ensure_ascii=False,
                                                       indent=2),
                                          encoding="utf-8")
                except Exception as e:
                    failed += 1
                    log.error("cache write failed %s: %s", cache_path.name, e)
                    status_rows.append({"file_path": fp, "status": "failed",
                                        "at": now_iso})
                    return
                extracted_multi += 1
                status_rows.append({"file_path": fp, "status": this_status,
                                    "at": now_iso})

            for chunk in chunked(todo, CHUNK_SIZE):
                if fatal:
                    break
                await asyncio.gather(*[one(w) for w in chunk],
                                     return_exceptions=True)
                done = extracted_single + extracted_multi + failed
                print(f"  [{done}/{len(todo)}] "
                      f"single={extracted_single}  multi={extracted_multi}  "
                      f"failed={failed}  mismatches={count_mismatches}")
            if fatal:
                raise fatal[0]

    write_extraction_status(status_rows)
    log.info(USAGE.summary("extract_descriptions"))

    return {"cache_hits": cache_hits,
            "extracted_single": extracted_single,
            "extracted_multi": extracted_multi,
            "failed": failed,
            "count_mismatches": count_mismatches}


def run(limit: int | None = None, force: bool = False,
        missing_only: bool = False) -> int:
    work = fetch_extraction_work(missing_only=missing_only)
    if limit:
        work = work[:limit]
    print(f"Worklist: {len(work)} Documents "
          f"(single={sum(1 for w in work if w['notice_type']=='single')}, "
          f"multi={sum(1 for w in work if w['notice_type']=='multi')})")
    if not work:
        return 0
    try:
        summary = asyncio.run(run_async(work, force=force))
    except RuntimeError as e:
        print(f"  ABORTED: {e}")
        return 1
    print(f"Done. cache_hits={summary['cache_hits']}  "
          f"single={summary['extracted_single']}  "
          f"multi={summary['extracted_multi']}  "
          f"failed={summary['failed']}  "
          f"mismatches={summary['count_mismatches']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N Documents")
    ap.add_argument("--force", action="store_true",
                    help="re-extract even when a cache file already exists")
    ap.add_argument("--missing-only", action="store_true",
                    help="restrict to Documents backing properties that lack a "
                         "notice-sourced description (safe backfill)")
    args = ap.parse_args()
    return run(limit=args.limit, force=args.force,
               missing_only=args.missing_only)


if __name__ == "__main__":
    sys.exit(main())
