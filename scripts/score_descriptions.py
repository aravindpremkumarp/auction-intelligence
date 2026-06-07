#!/usr/bin/env python3
"""Score the full corpus of notice-extracted descriptions with the LLM judge.

Runs the (scoped) completeness judge over every property that has a notice
extraction (description_source in notice/human), concurrently, and writes one
JSON line per property to output/description_judge.jsonl. Resumable: rows already
in the output file are skipped, so it can be re-run after interruption.

This is the "score the full corpus first" step — it does NOT write to Neo4j.
Inspect the JSONL / printed distribution, then we persist + build the UI.

    NEO4J_HTTP_API=1 OPENROUTER_MODEL=deepseek/deepseek-v4-pro \
        python -m scripts.score_descriptions --workers 6
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from api.neo4j_client import run_read_query
from scripts.dryrun_description_completeness import (
    FETCH,
    JUDGE_PROMPT,
    build_identity,
    build_row,
    text_overlap,
)

OUT = Path("output/description_judge.jsonl")


def judge(markdown: str, extracted: str, identity: str, retries: int = 3) -> dict | None:
    """Robust single judge call: retries on transient/parse failure."""
    from pipeline.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL
    from pipeline.ocr_extract import parse_llm_response

    prompt = JUDGE_PROMPT.format(
        identity=identity or "the single property described in this notice",
        markdown=(markdown or "")[:20000],
        extracted=extracted or "",
    )
    payload = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        # deepseek-v4-pro and other reasoning models spend tokens on a
        # reasoning pass before emitting the answer; 1024 was being consumed
        # by reasoning, leaving content empty (null) → judge failed. Give the
        # model headroom to actually produce the small JSON verdict.
        "max_tokens": 4096,
        "temperature": 0.0,
    }).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                data=payload,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://bank-auction-intelligence.local",
                },
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
            parsed = parse_llm_response(data["choices"][0]["message"]["content"])
            if parsed is not None:
                return parsed
        except Exception:
            pass
        time.sleep(2 ** attempt)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="cap rows scored (0 = all)")
    args = ap.parse_args()

    OUT.parent.mkdir(exist_ok=True)
    done = set()
    if OUT.exists():
        for line in OUT.read_text().splitlines():
            try:
                done.add(json.loads(line)["auction_id"])
            except Exception:
                pass

    rows = [build_row(r) for r in run_read_query(FETCH, {}, timeout=120, max_rows=100000)]
    todo = [r for r in rows if r["has_extracted"] and r["source"] in ("notice", "human")
            and r["auction_id"] not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"corpus: {len(rows)} props · already scored: {len(done)} · to score now: {len(todo)}",
          flush=True)
    if not todo:
        print("nothing to do.")
        return 0

    def work(r: dict) -> dict:
        identity = build_identity(r["borrowers"], r["reserve_price"], r["emd"])
        v = judge(r["_markdown"], r["_extracted"], identity)
        return {
            "auction_id": r["auction_id"],
            "notice_type": r["notice_type"],
            "source": r["source"],
            "text_overlap": r["text_overlap"],
            "ok": v is not None,
            "completeness": (v or {}).get("completeness"),
            "complete": (v or {}).get("complete"),
            "wrong_property": (v or {}).get("wrong_property"),
            "confidence": (v or {}).get("confidence"),
            "missing_parts": (v or {}).get("missing_parts"),
            "reasoning": (v or {}).get("reasoning"),
        }

    n = 0
    t0 = time.time()
    with OUT.open("a") as f, ThreadPoolExecutor(max_workers=args.workers) as ex:
        for fut in as_completed([ex.submit(work, r) for r in todo]):
            rec = fut.result()
            f.write(json.dumps(rec) + "\n")
            f.flush()
            n += 1
            if n % 20 == 0 or n == len(todo):
                rate = n / max(time.time() - t0, 1)
                print(f"  scored {n}/{len(todo)}  ({rate:.1f}/s)", flush=True)

    print(f"done: wrote {n} rows to {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
