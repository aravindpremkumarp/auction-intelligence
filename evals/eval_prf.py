"""Side-by-side: legacy recall-only grader vs the new precision-aware grader.

Extracts each gold notice ONCE, caches the raw entity records to
evals/.prf_cache.json (so re-scoring is free), then prints:

  OLD  = evals.langextract_eval overall recall accuracy (the current number)
  NEW  = evals.prf_score precision / recall / F1 + over-emission / slotting

Run once (spends a little API budget, passes=1):
    NEO4J_HTTP_API=1 LANGEXTRACT_PASSES=1 python -m evals.eval_prf
Re-score from cache (free):
    python -m evals.eval_prf --cached
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from evals.langextract_eval import (FIX, load_gold, score_records, _records)
from evals import prf_score

CACHE = Path(__file__).resolve().parent / ".prf_cache.json"


def _extract_all(gold) -> dict[str, list[dict]]:
    """Live-extract every fixture once, routing the model exactly like
    production (pipeline/extract_routing) instead of LangExtract's default
    LANGEXTRACT_MODEL_ID — so the eval scores the SAME model the pipeline runs
    (single -> hy3, multi -> deepseek), not gemini."""
    from pipeline import langextract_run as LR
    from pipeline import langextract_examples as LX
    from pipeline.extract_routing import select_extract_model
    LR.install_usage_tracking()
    out: dict[str, list[dict]] = {}
    for g in gold:
        md = (FIX / f"{g['aid']}.txt").read_text(encoding="utf-8")
        # Route on the gold notice_type.
        model_id, reasoning_off = select_extract_model(g.get("notice_type"))
        LR.USAGE.docs += 1
        res = None
        for _ in range(3):                      # retry transient empty response
            res = LX.extract(md, model_id=model_id, reasoning_off=reasoning_off)
            if res.extractions:
                break
        out[g["aid"]] = _records(res)
        print(f"  extracted {g['aid']} [{model_id}]: {len(out[g['aid']])} entities")
    print("\n=== EXTRACTION COST ===")
    print(LR.USAGE.report())
    return out


def main() -> int:
    cached = "--cached" in sys.argv
    os.environ.setdefault("LANGEXTRACT_PASSES", "1")
    gold = load_gold()

    if cached and CACHE.exists():
        records_by_aid = json.loads(CACHE.read_text(encoding="utf-8"))
        print(f"loaded cached extractions for {len(records_by_aid)} notices\n")
    else:
        records_by_aid = _extract_all(gold)
        CACHE.write_text(json.dumps(records_by_aid, ensure_ascii=False), encoding="utf-8")
        print(f"\ncached -> {CACHE.name}\n")

    # OLD — legacy recall-only accuracy (reproduces evals.langextract_eval).
    total = correct = 0
    for g in gold:
        rows, _ = score_records(g, records_by_aid.get(g["aid"], []))
        correct += sum(1 for *_, ok in rows if ok)
        total += len(rows)
    old_acc = correct / total * 100 if total else 0.0

    # NEW — precision-aware.
    prf = prf_score.score_prf(gold, records_by_aid)

    print("=" * 68)
    print(f"OLD  (recall-only)     : {old_acc:5.1f}%   ({correct}/{total} gold values found)")
    print(f"NEW  precision         : {prf.precision*100:5.1f}%")
    print(f"NEW  recall            : {prf.recall*100:5.1f}%")
    print(f"NEW  F1                : {prf.f1*100:5.1f}%")
    print("=" * 68)
    print(prf_score.report(prf))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
