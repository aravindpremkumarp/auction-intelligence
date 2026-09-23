"""Measure lot recall of the current extraction path against what is stored.

READ-ONLY: runs the model and writes nothing to the graph. Every notice is read
through ``reset_langextract_and_extract.read_notice`` — the exact path a
re-extraction writes — and compared with the extraction already stored on it.

Why it exists
-------------
A change to the prompt examples reaches every notice, single-lot ones included,
and a change to chunking reaches every long one. "Found more lots on the notices
that were short" is only half the question; the other half is "and made no
notice that was already right any worse". So the sets are:

    short    notices holding fewer :Lot nodes than the reviewer's lot count
    chunked  multi-lot notices already complete that pipeline/lot_chunks can cut
    multi    multi-lot notices already complete that are still read whole
    single   single-lot notices

Per notice it reports expected lots, lots in the stored extraction, lots in the
new one, and the validators.py score of each (the same scorer on both, so the
comparison is not skewed by whatever SCORE_VERSION the stored one carries).

Run:
    NEO4J_HTTP_API=1 LANGEXTRACT_PROVIDER=openrouter \\
        python -m scripts.eval_lot_recall --short --chunked 20 --multi 10 \\
        --single 20 --concurrency 6 --out evals/lot_recall/run.jsonl
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from api.neo4j_client import run_read_query
from pipeline.lot_chunks import lots_read, plan_chunks
from pipeline.validators import validate_stored
from scripts.reset_langextract_and_extract import read_notice, select_only_docs

_BASE = (
    "MATCH (d:Document) "
    "WHERE d.stitched_into IS NULL AND d.extraction_json IS NOT NULL "
    "  AND d.markdown IS NOT NULL AND d.markdown <> '' "
    "OPTIONAL MATCH (d)-[:HAS_LOT]->(l:Lot) "
    "WITH d, count(l) AS lots, "
    "     coalesce(d.stitched_expected_lot_count, d.expected_lot_count) AS elc "
)


def _names(where: str, limit: int, params: dict | None = None) -> list[dict]:
    return run_read_query(
        _BASE + f"WHERE {where} "
        "RETURN d.filename AS f, coalesce(d.stitched_markdown, d.markdown) AS md, "
        "       elc ORDER BY d.filename",
        params or {}, max_rows=20_000, timeout=120.0)[: limit if limit else None]


def pick(args) -> dict[str, list[str]]:
    rng = random.Random(args.seed)
    sets: dict[str, list[str]] = {}
    if args.short:
        sets["short"] = [r["f"] for r in _names("elc IS NOT NULL AND lots < elc", 0)]
    if args.chunked or args.multi:
        done = _names("d.notice_type = 'multi' AND elc IS NOT NULL AND lots = elc", 0)
        chunkable = [r["f"] for r in done if plan_chunks(r["md"], r["elc"])]
        whole = [r["f"] for r in done if not plan_chunks(r["md"], r["elc"])]
        if args.chunked:
            sets["chunked"] = rng.sample(chunkable, min(args.chunked, len(chunkable)))
        if args.multi:
            sets["multi"] = rng.sample(whole, min(args.multi, len(whole)))
    if args.single:
        singles = [r["f"] for r in _names(
            "coalesce(d.notice_type, 'single') <> 'multi' AND lots = 1", 0)]
        sets["single"] = rng.sample(singles, min(args.single, len(singles)))
    if args.only:
        sets["named"] = list(args.only)
    return sets


def _stored(filename: str) -> list[dict]:
    rows = run_read_query(
        "MATCH (d:Document {filename: $f}) RETURN d.extraction_json AS j",
        {"f": filename})
    try:
        return json.loads(rows[0]["j"]) if rows and rows[0]["j"] else []
    except ValueError:
        return []


def measure(d: dict, group: str) -> dict:
    row = {"group": group, "filename": d["filename"],
           "expected": d.get("expected_lot_count"),
           "layout": (p.strategy if (p := plan_chunks(d["md"],
                                                      d.get("expected_lot_count")))
                      else "whole")}
    old = _stored(d["filename"])
    row["old_lots"] = lots_read(old)
    row["old_score"] = validate_stored(old, source_text=d["md"])["score"] if old else None
    t0 = time.time()
    try:
        new, _model = read_notice(d, route=True)
    except Exception as e:  # a failed read is a result, not a crash
        row.update(error=f"{type(e).__name__}: {e}", seconds=round(time.time() - t0))
        return row
    row["seconds"] = round(time.time() - t0)
    row["new_lots"] = lots_read(new)
    row["new_score"] = validate_stored(new, source_text=d["md"])["score"] if new else None
    row["entities"] = new
    return row


def summarize(rows: list[dict]) -> str:
    lines = [f"{'group':8} {'n':>3} {'expected':>8} {'old lots':>8} {'new lots':>8} "
             f"{'better':>6} {'worse':>5} {'old score':>9} {'new score':>9} {'errors':>6}"]
    for g in sorted({r["group"] for r in rows}):
        rs = [r for r in rows if r["group"] == g]
        ok = [r for r in rs if "error" not in r]
        exp = sum(r["expected"] or 0 for r in ok)
        old = sum(r["old_lots"] for r in ok)
        new = sum(r["new_lots"] for r in ok)
        # Closer to the confirmed count is better — in either direction, since
        # an over-split notice (9 lots for 7) is as wrong as a short one.
        def gap(r, k):
            return abs((r["expected"] or 1) - r[k])
        better = sum(gap(r, "new_lots") < gap(r, "old_lots") for r in ok)
        worse = sum(gap(r, "new_lots") > gap(r, "old_lots") for r in ok)

        def avg(k):
            v = [r[k] for r in ok if r.get(k) is not None]
            return f"{sum(v) / len(v):.1f}" if v else "-"
        lines.append(f"{g:8} {len(rs):>3} {exp:>8} {old:>8} {new:>8} {better:>6} "
                     f"{worse:>5} {avg('old_score'):>9} {avg('new_score'):>9} "
                     f"{len(rs) - len(ok):>6}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--short", action="store_true",
                    help="every notice holding fewer lots than its confirmed count")
    ap.add_argument("--chunked", type=int, default=0,
                    help="sample N complete multi-lot notices that get chunked")
    ap.add_argument("--multi", type=int, default=0,
                    help="sample N complete multi-lot notices still read whole")
    ap.add_argument("--single", type=int, default=0,
                    help="sample N single-lot notices")
    ap.add_argument("--only", action="append", metavar="FILENAME",
                    help="also measure this notice (repeatable)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", required=True,
                    help="JSONL of per-notice results (new entities included)")
    args = ap.parse_args()

    sets = pick(args)
    work = []
    for group, names in sets.items():
        for d in select_only_docs(names):
            work.append((d, group))
    print(f"measuring {len(work)} notice(s): "
          + ", ".join(f"{g}={len(n)}" for g, n in sets.items()), flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex, \
            out.open("w", encoding="utf-8") as fh:
        futs = {ex.submit(measure, d, g): d["filename"] for d, g in work}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            rows.append(r)
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            fh.flush()
            print(f"  [{i}/{len(work)}] {r['group']:7} {r['filename'][:44]:44} "
                  f"exp={r['expected']} old={r['old_lots']} "
                  f"new={r.get('new_lots', 'ERR')} score {r['old_score']}"
                  f"->{r.get('new_score', '-')} {r['layout']} "
                  f"{r.get('error', '')}", flush=True)
    print("\n" + summarize(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
