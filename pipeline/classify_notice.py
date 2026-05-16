"""Classify each :Document as 'single' or 'multi' property notice.

Two-pass design:

  Pass 1: cluster count
      Count how many :AuctionProperty rows link to each :Document via
      [:HAS_DOCUMENT]. Set notice_type = 'single' when pc=1 else 'multi',
      and property_count = pc. This is the historical signal from
      scripts/classify_notices.py — preserved here so the cluster
      tagging keeps working even when the LLM cannot run.

  Pass 2: LLM on the markdown
      For each Document with cached MinerU markdown that the human has
      NOT already overridden, call the classifier LLM. Write the LLM's
      verdict to a separate field set:
          notice_type_classifier_pred   ('single' | 'multi')
          notice_type_confidence        float 0..1
          notice_type_reasoning         short string
          notice_type_model             which model produced it
          notice_type_classified_at     datetime

  Disagreement is not stored as a flag — it is the predicate
  ``notice_type <> notice_type_classifier_pred`` used by the review queue
  to surface notices that need human attention.

  Manual-override guard. Documents flagged with notice_type_overridden=true
  are NEVER overwritten by pass 1; pass 2 still updates the LLM's prediction
  for transparency, but the canonical notice_type stays where the human put it.

Run:  python -m pipeline.classify_notice

Idempotent. Re-runs reuse the per-document classification cache at
pipeline/cache/classifications/<safe_path>.json.
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
from pipeline.config import (
    CLASSIFY_CACHE_DIR,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL_CLASSIFY,
    PROMPTS_DIR,
    MAX_RETRIES,
)


load_dotenv()

PROMPT_PATH = PROMPTS_DIR / "classify_notice.txt"
LLM_CONCURRENCY = 6
CHUNK_SIZE = 50
VALID_LABELS = ("single", "multi")


def safe_cache_name(file_path: str) -> str:
    return file_path.replace("/", "_").replace("\\", "_").replace(":", "_")


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def stamp_cluster_counts() -> int:
    """Pass 1: seed notice_type + property_count from cluster size.

    Skips Documents the human has overridden. Returns the number stamped.
    """
    res = run_query("""
        MATCH (d:Document)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
        WITH d, count(DISTINCT a) AS pc
        WHERE coalesce(d.notice_type_overridden, false) = false
        SET d.property_count = pc,
            d.notice_type = CASE WHEN pc = 1 THEN 'single' ELSE 'multi' END
        RETURN count(d) AS n
    """)
    return int(res[0]["n"]) if res else 0


def fetch_classify_work() -> list[dict]:
    """Every Document the LLM still needs to score.

    Skips Documents that already have an LLM prediction (cached or stored)
    so re-runs are cheap; skips Documents without markdown (MinerU hasn't
    run yet).
    """
    return run_read_query("""
      MATCH (d:Document)
      WHERE d.markdown IS NOT NULL
        AND d.markdown <> ''
      OPTIONAL MATCH (d)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
      WITH d, count(DISTINCT a) AS pc
      RETURN d.file_path                AS file_path,
             d.filename                 AS filename,
             d.markdown                 AS markdown,
             coalesce(d.property_count, pc) AS property_count,
             d.notice_type              AS notice_type,
             d.notice_type_classifier_pred AS prior_pred
    """, max_rows=20_000, timeout=30.0)


def parse_llm_json(text: str | None) -> dict | None:
    if not text:
        return None
    text = text.strip()
    if not text:
        return None
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


def normalize_verdict(obj: dict) -> dict | None:
    """Validate LLM output. Returns {classification, confidence, reasoning}
    or None on bad shape."""
    label = obj.get("classification")
    if not isinstance(label, str) or label.strip().lower() not in VALID_LABELS:
        return None
    label = label.strip().lower()
    conf = obj.get("confidence")
    if isinstance(conf, (int, float)):
        conf = float(conf)
        # Clamp accidentally-out-of-range values rather than rejecting them.
        if conf > 1.0:
            conf = 1.0
        if conf < 0.0:
            conf = 0.0
    else:
        conf = None
    reasoning = obj.get("reasoning")
    if not isinstance(reasoning, str):
        reasoning = ""
    return {
        "classification": label,
        "confidence": conf,
        "reasoning": reasoning[:1000],  # cap so we don't bloat Neo4j rows
    }


async def call_llm(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    markdown: str,
    cluster_count: int,
    prompt_template: str,
) -> dict | None:
    prompt = prompt_template.replace("{cluster_count}", str(cluster_count or "unknown"))
    full = (
        prompt
        + "\n\n---\nThe document text below was extracted by a layout-aware "
          "OCR tool (MinerU). Read it carefully and count distinct reserve "
          "prices.\n\n"
        + markdown
    )
    payload = {
        "model": OPENROUTER_MODEL_CLASSIFY,
        "messages": [{"role": "user", "content": full}],
        # Reasoning models consume most of the budget on chain-of-thought
        # (observed ~500 reasoning tokens on long notices); keep enough
        # headroom for the JSON verdict so finish_reason isn't 'length'.
        "max_tokens": 2048,
        "temperature": 0.0,
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
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 404:
                        body = await resp.text()
                        raise RuntimeError(
                            f"OpenRouter 404 for model "
                            f"'{OPENROUTER_MODEL_CLASSIFY}'. Body: {body[:200]}"
                        )
                    if resp.status == 403:
                        body = await resp.text()
                        raise RuntimeError(
                            f"OpenRouter 403 (auth/key limit) for "
                            f"'{OPENROUTER_MODEL_CLASSIFY}'. Body: {body[:200]}"
                        )
                    if resp.status != 200:
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(2 ** attempt)
                        continue
                    data = await resp.json()
                    text = data["choices"][0]["message"]["content"]
                    obj = parse_llm_json(text)
                    if obj is None:
                        return None
                    return normalize_verdict(obj)
            except RuntimeError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
        return None


def write_predictions_to_neo4j(payload: list[dict]) -> int:
    """Upsert LLM predictions onto Document nodes in batches."""
    if not payload:
        return 0
    n = 0
    for batch in chunked(payload, 200):
        run_query("""
            UNWIND $rows AS row
            MATCH (d:Document {file_path: row.file_path})
            SET d.notice_type_classifier_pred = row.classification,
                d.notice_type_confidence      = row.confidence,
                d.notice_type_reasoning       = row.reasoning,
                d.notice_type_model           = row.model,
                d.notice_type_classified_at   = datetime(row.classified_at)
        """, {"rows": batch})
        n += len(batch)
    return n


async def run_llm_pass(work: list[dict], force: bool = False) -> dict:
    """Pass 2. Returns summary dict."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY missing")
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    CLASSIFY_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    todo: list[dict] = []
    cache_hits = 0
    cached_verdicts: dict[str, dict] = {}

    for w in work:
        if w.get("prior_pred") and not force:
            # Already scored at some point — skip unless we're forcing a re-score.
            continue
        cache_path = CLASSIFY_CACHE_DIR / f"{safe_cache_name(w['file_path'])}.json"
        if not force and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                v = normalize_verdict(cached.get("verdict") or {})
            except Exception:
                v = None
            if v is not None:
                cache_hits += 1
                cached_verdicts[w["file_path"]] = {**v,
                                                   "model": cached.get("model") or OPENROUTER_MODEL_CLASSIFY,
                                                   "classified_at": cached.get("classified_at")}
                continue
        todo.append(w)

    print(f"  cache_hits={cache_hits}  to_classify={len(todo)}")

    new_results: dict[str, dict] = {}
    if todo:
        sem = asyncio.Semaphore(LLM_CONCURRENCY)
        connector = aiohttp.TCPConnector(limit=LLM_CONCURRENCY * 2)
        failed = 0
        async with aiohttp.ClientSession(connector=connector) as session:
            fatal: list[BaseException] = []

            async def one(w: dict):
                nonlocal failed
                if fatal:
                    return
                fp = w["file_path"]
                try:
                    verdict = await call_llm(session, sem, w["markdown"],
                                              int(w.get("property_count") or 0),
                                              prompt)
                except RuntimeError as e:
                    if not fatal:
                        fatal.append(e)
                        print(f"\n  FATAL: {e}")
                    return
                except Exception as e:
                    failed += 1
                    print(f"  [LLM-fail] {safe_cache_name(fp)[:60]}: {e}")
                    return
                if verdict is None:
                    failed += 1
                    return
                now = datetime.now(timezone.utc).isoformat()
                cache_path = CLASSIFY_CACHE_DIR / f"{safe_cache_name(fp)}.json"
                try:
                    cache_path.write_text(json.dumps({
                        "verdict": verdict,
                        "model": OPENROUTER_MODEL_CLASSIFY,
                        "classified_at": now,
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass
                new_results[fp] = {**verdict,
                                   "model": OPENROUTER_MODEL_CLASSIFY,
                                   "classified_at": now}

            for chunk in chunked(todo, CHUNK_SIZE):
                if fatal:
                    break
                await asyncio.gather(*[one(w) for w in chunk], return_exceptions=True)
                print(f"  [{len(new_results) + failed}/{len(todo)}] "
                      f"scored={len(new_results)}  failed={failed}")
            if fatal:
                raise fatal[0]

    # Write to Neo4j
    now_iso = datetime.now(timezone.utc).isoformat()
    payload: list[dict] = []
    for fp, v in {**cached_verdicts, **new_results}.items():
        payload.append({
            "file_path": fp,
            "classification": v["classification"],
            "confidence": v["confidence"],
            "reasoning": v["reasoning"],
            "model": v.get("model") or OPENROUTER_MODEL_CLASSIFY,
            "classified_at": v.get("classified_at") or now_iso,
        })
    written = write_predictions_to_neo4j(payload)
    print(f"  wrote {written} predictions to Neo4j")
    return {"cached": cache_hits, "scored": len(new_results),
            "written": written}


def print_summary() -> None:
    print()
    print("notice_type distribution:")
    for r in run_read_query("""
      MATCH (d:Document)
      WHERE d.notice_type IS NOT NULL
      RETURN d.notice_type AS notice_type,
             count(*) AS docs,
             sum(d.property_count) AS properties
      ORDER BY notice_type
    """):
        print(f"  {r['notice_type']:<6} docs={r['docs']:>5}  "
              f"properties={r['properties']:>5}")

    print()
    print("Classifier agreement with cluster count:")
    for r in run_read_query("""
      MATCH (d:Document)
      WHERE d.notice_type_classifier_pred IS NOT NULL
      WITH (d.notice_type = d.notice_type_classifier_pred) AS agrees
      RETURN agrees, count(*) AS n
      ORDER BY agrees DESC
    """):
        label = "agree" if r["agrees"] else "DISAGREE"
        print(f"  {label:<10} {r['n']:>5}")

    print()
    print("Pending classification review:")
    for r in run_read_query("""
      MATCH (d:Document)
      WHERE d.notice_type IS NOT NULL
      RETURN
        sum(CASE WHEN coalesce(d.notice_type_overridden, false) = false
                  AND d.notice_type_verified_at IS NULL THEN 1 ELSE 0 END) AS pending,
        sum(CASE WHEN d.notice_type_verified_at IS NOT NULL THEN 1 ELSE 0 END) AS verified,
        sum(CASE WHEN coalesce(d.notice_type_overridden, false) = true THEN 1 ELSE 0 END) AS overridden
    """):
        print(f"  pending={r['pending']}  verified={r['verified']}  "
              f"overridden={r['overridden']}")


def run(limit: int | None = None, force: bool = False,
        skip_llm: bool = False) -> int:
    """Public entry point used by pipeline/run_pipeline.py."""
    print("Pass 1: cluster-count classification")
    tagged = stamp_cluster_counts()
    print(f"  tagged {tagged} Documents (overrides preserved)")

    if skip_llm:
        print("Pass 2: SKIPPED (--skip-llm)")
        print_summary()
        return 0

    print("Pass 2: LLM classification on markdown")
    if not OPENROUTER_API_KEY:
        print("  OPENROUTER_API_KEY missing — skipping pass 2")
        print_summary()
        return 0
    work = fetch_classify_work()
    if limit:
        work = work[:limit]
    print(f"  worklist: {len(work)} Documents with markdown")
    if not work:
        print_summary()
        return 0
    try:
        asyncio.run(run_llm_pass(work, force=force))
    except RuntimeError as e:
        print(f"  ABORTED: {e}")
        return 1
    print_summary()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N Documents")
    ap.add_argument("--force", action="store_true",
                    help="re-score even when a prior LLM prediction exists")
    ap.add_argument("--skip-llm", action="store_true",
                    help="only run pass 1 (cluster-count tagging)")
    args = ap.parse_args()
    return run(limit=args.limit, force=args.force, skip_llm=args.skip_llm)


if __name__ == "__main__":
    sys.exit(main())
