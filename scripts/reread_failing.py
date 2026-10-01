"""Re-read the notices whose stored extraction is failing — and only those.

The corpus is not re-read wholesale. A notice is re-read when its stored
read is demonstrably short: a low validator score, a lot count below the
reviewer's, a failing issue code (lot under-recall, an incomplete or
missing description, ungrounded values, a missing borrower, a cross-field
contradiction), a missing key fact, or a staleness stamp. Each one goes
through the chosen reader (pipeline/extract_entry) and the keep-better gate
(pipeline/extraction_store), so a re-read can only ever improve a notice.

``--dry-run`` prints what would be read: the count by reason, how many carry
reviewer corrections (the orphan forecast), and a cost estimate from the
last shadow cycle's cost per notice when one exists. A before/after report
(validator score and key-fact fill, by reason) goes to
evals/corpus_runs/<ts>/reread.json.

Run:  NEO4J_HTTP_API=1 python -m scripts.reread_failing --dry-run
      NEO4J_HTTP_API=1 python -m scripts.reread_failing --reader v2 --concurrency 4
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from api.neo4j_client import run_read_query
from pipeline import extraction_store as ES
from pipeline.config import EXTRACT_MIN_OCR_HEALTH
from pipeline.extract_entry import read_document
from pipeline.load_extractions import ROSTER_CYPHER, _next_batch, _plan_groups

SCORE_BELOW = 70
FAILING_CODES = ("lot_under_recall", "full_description_incomplete", "missing_full_description",
                 "ungrounded", "lot_missing_borrower", "cross_field_inconsistent",
                 "contested_key_fact")

#: Why a notice is selected, in priority order; the first that holds names it.
REASONS = (
    ("stale", "d.extraction_stale_at IS NOT NULL"),
    ("lot_count", "coalesce(d.extraction_lot_count < "
                  "coalesce(d.stitched_expected_lot_count, d.expected_lot_count), false)"),
    ("score", f"coalesce(d.extraction_score < {SCORE_BELOW}, false)"),
    ("issue", "any(c IN coalesce(d.extraction_issue_codes, []) WHERE c IN $codes)"),
    ("missing_key", "coalesce(d.extraction_key_missing > 0, false)"),
)

SELECT = """
MATCH (d:Document)
WHERE d.extraction_json IS NOT NULL AND d.stitched_into IS NULL
  AND d.markdown IS NOT NULL AND d.markdown <> ''
  AND (d.ocr_health_score IS NULL OR d.ocr_health_score >= $min_ocr)
  AND (__PREDICATE__)
""" + ROSTER_CYPHER + """
RETURN d.filename AS filename,
       coalesce(d.stitched_markdown, d.markdown) AS md,
       d.notice_type AS notice_type,
       coalesce(d.stitched_expected_lot_count, d.expected_lot_count) AS expected_lot_count,
       d.blocks AS blocks, roster AS roster,
       d.extraction_score AS score_before, d.extraction_key_missing AS key_missing_before,
       d.extraction_corrections_json IS NOT NULL AND d.extraction_corrections_json <> '{}' AS has_corrections,
       CASE __REASONS__ END AS reason,
       d.extraction_reader AS reader_before
ORDER BY coalesce(d.extraction_score, 0) ASC, d.filename
__LIMIT__
"""

AFTER = """
UNWIND $fns AS fn MATCH (d:Document {filename: fn})
RETURN fn, d.extraction_score AS score, d.extraction_key_missing AS key_missing,
       d.extraction_reader AS reader, d.extraction_contested AS contested
"""

SHADOW_COST = """
MATCH (d:Document) WHERE d.extraction_shadow_telemetry_json IS NOT NULL
RETURN d.extraction_shadow_telemetry_json AS tel LIMIT 500
"""


def select(limit: int | None, only: list[str] | None, min_ocr: int) -> list[dict]:
    predicate = " OR ".join(f"({p})" for _, p in REASONS)
    reason_case = " ".join(f"WHEN {p} THEN '{name}'" for name, p in REASONS) + " ELSE 'none'"
    if only:
        predicate = "d.filename IN $only"
    # plain replacement, not str.format: the roster fragment holds Cypher maps
    q = (SELECT.replace("__PREDICATE__", predicate).replace("__REASONS__", reason_case)
         .replace("__LIMIT__", f"LIMIT {int(limit)}" if limit else ""))
    return run_read_query(q, {"codes": list(FAILING_CODES), "min_ocr": int(min_ocr),
                              "only": only or []}, max_rows=50_000, timeout=300.0)


def cost_per_notice() -> float | None:
    rows = run_read_query(SHADOW_COST, {})
    costs = []
    for r in rows or []:
        try:
            c = json.loads(r["tel"] or "{}").get("cost_usd")
            if c:
                costs.append(float(c))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return statistics.mean(costs) if costs else None


def forecast(docs: list[dict], unit_cost: float | None) -> dict:
    return {"documents": len(docs),
            "by_reason": dict(Counter(d.get("reason") for d in docs)),
            "with_corrections": sum(1 for d in docs if d.get("has_corrections")),
            "unit_cost_usd": unit_cost,
            "cost_usd": round(unit_cost * len(docs), 2) if unit_cost else None}


def _one(d: dict, batch: int, reader: str) -> tuple[str, str]:
    try:
        ents, model, meta = read_document(d, True, reader=reader)
        if not ents:
            return d["filename"], "fail: no entities"
        res = ES.write_extraction(d, ents, batch, reader=meta.get("reader", reader), model=model,
                                  keep_better=True, meta=meta)
        return d["filename"], f"{res['how']} score={res['score']}"
    except ES.KeptExisting as e:
        return d["filename"], f"kept: {e}"
    except Exception as e:  # noqa: BLE001 - one notice must not stop the run
        return d["filename"], f"fail: {type(e).__name__}: {str(e)[:160]}"


def run(docs: list[dict], reader: str, concurrency: int) -> dict:
    batch = _next_batch()
    leaders, _ = _plan_groups(docs, force=True, batch=batch)
    for d in leaders:
        d["keep_better"] = True
    outcomes: Counter = Counter()
    lines = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futs = {pool.submit(_one, d, batch, reader): d for d in leaders}
        for i, fut in enumerate(as_completed(futs), 1):
            fn, line = fut.result()
            outcomes[line.split(":")[0].split(" ")[0]] += 1
            lines.append(f"{fn}: {line}")
            print(f"  [{i}/{len(leaders)}] {fn}: {line}", flush=True)
    after = {r["fn"]: r for r in run_read_query(AFTER, {"fns": [d["filename"] for d in docs]})}
    before_s = [d["score_before"] for d in docs if d.get("score_before") is not None]
    after_s = [after[d["filename"]]["score"] for d in docs
               if d["filename"] in after and after[d["filename"]].get("score") is not None]
    return {"batch": batch, "reader": reader, "documents": len(docs), "pages_read": len(leaders),
            "outcomes": dict(outcomes), "seconds": round(time.monotonic() - t0, 1),
            "score_before_mean": round(statistics.mean(before_s), 1) if before_s else None,
            "score_after_mean": round(statistics.mean(after_s), 1) if after_s else None,
            "key_missing_before": sum(int(d.get("key_missing_before") or 0) for d in docs),
            "key_missing_after": sum(int((after.get(d["filename"]) or {}).get("key_missing") or 0)
                                     for d in docs),
            "contested_after": sum(int((after.get(d["filename"]) or {}).get("contested") or 0)
                                   for d in docs),
            "by_reason": dict(Counter(d.get("reason") for d in docs)),
            "lines": lines}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reader", choices=("v2", "langextract"), default="v2")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", nargs="*", help="these Document.filename values only")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--min-ocr", type=int, default=EXTRACT_MIN_OCR_HEALTH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    docs = select(args.limit, args.only, args.min_ocr)
    fc = forecast(docs, cost_per_notice())
    print(json.dumps(fc, indent=1))
    if args.dry_run or not docs:
        return 0
    rep = run(docs, args.reader, args.concurrency)
    out = Path("evals/corpus_runs") / time.strftime("%Y%m%d-%H%M%S") / "reread.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"forecast": fc, **rep}, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in rep.items() if k != "lines"}, indent=1))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
