"""What the v2 reader would have written, measured against what v1 wrote.

A cron cycle with EXTRACT_READER=shadow (pipeline/extract_entry) stores the
v2 read beside the v1 one on every document it extracts
(extraction_shadow_json / _score / _judge / _telemetry_json). This report
aggregates them: how often v2 is better by the keep-better gate and what it
gains and loses, validator scores side by side, lot-count agreement with
the reviewer's count, ungrounded entities on each side, the evidence mix v2
reports, dropped quotes, seconds and cost per filled key fact. It reads;
it writes nothing.

Run:  NEO4J_HTTP_API=1 python -m scripts.reader_shadow_report [--limit 500] [--json out.json]
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys

from scripts.score_ink_coverage import nq

QUERY = """
MATCH (d:Document) WHERE d.extraction_shadow_at IS NOT NULL
RETURN d.filename AS fn, d.extraction_json AS v1, d.extraction_shadow_json AS v2,
       d.extraction_score AS s1, d.extraction_shadow_score AS s2,
       d.extraction_shadow_judge AS judge, d.extraction_shadow_telemetry_json AS tel,
       d.extraction_shadow_error AS err, d.extraction_shadow_seconds AS secs,
       coalesce(d.stitched_expected_lot_count, d.expected_lot_count) AS expected,
       d.notice_type AS notice_type
ORDER BY d.extraction_shadow_at DESC LIMIT $limit
"""


def _lots(j: str | None) -> int:
    try:
        ents = json.loads(j or "[]")
    except json.JSONDecodeError:
        return 0
    return len({str((e.get("attrs") or {}).get("lot_index") or "1") for e in ents
                if e.get("cls") not in ("secured_creditor", "contact", "emd_account", "full_terms")})


def _ungrounded(j: str | None) -> int:
    try:
        return sum(1 for e in json.loads(j or "[]") if e.get("start") is None)
    except json.JSONDecodeError:
        return 0


def report(rows: list[dict]) -> dict:
    out = {"documents": len(rows), "errors": 0, "v2_better": 0, "v2_not_better": 0,
           "gains": collections.Counter(), "losses": collections.Counter(),
           "score": {"v1": [], "v2": []}, "lot_count": {"v1_ok": 0, "v2_ok": 0, "with_count": 0},
           "ungrounded": {"v1": 0, "v2": 0}, "evidence": collections.Counter(),
           "dropped": 0, "seconds": [], "cost_usd": 0.0, "key_filled": 0, "key_verified": 0}
    for r in rows:
        if r.get("err"):
            out["errors"] += 1
            continue
        try:
            judge = json.loads(r.get("judge") or "{}")
        except json.JSONDecodeError:
            judge = {}
        out["v2_better" if judge.get("v2_better") else "v2_not_better"] += 1
        out["gains"].update(g.split(":")[0] if ":" in g else g for g in judge.get("gains", []))
        out["losses"].update(g.split(":")[0] if ":" in g else g for g in judge.get("losses", []))
        if r.get("s1") is not None:
            out["score"]["v1"].append(r["s1"])
        if r.get("s2") is not None:
            out["score"]["v2"].append(r["s2"])
        if r.get("expected"):
            out["lot_count"]["with_count"] += 1
            out["lot_count"]["v1_ok"] += int(_lots(r.get("v1")) == int(r["expected"]))
            out["lot_count"]["v2_ok"] += int(_lots(r.get("v2")) == int(r["expected"]))
        out["ungrounded"]["v1"] += _ungrounded(r.get("v1"))
        out["ungrounded"]["v2"] += _ungrounded(r.get("v2"))
        try:
            tel = json.loads(r.get("tel") or "{}")
        except json.JSONDecodeError:
            tel = {}
        out["dropped"] += int(tel.get("dropped") or 0)
        out["key_filled"] += int(tel.get("key_filled") or 0)
        out["key_verified"] += int(tel.get("key_verified") or 0)
        out["cost_usd"] += float(tel.get("cost_usd") or 0)
        if r.get("secs") is not None:
            out["seconds"].append(float(r["secs"]))
        try:
            for e in json.loads(r.get("v2") or "[]"):
                out["evidence"][(e.get("attrs") or {}).get("evidence") or "EXPLICIT"] += 1
        except json.JSONDecodeError:
            pass
    for k in ("v1", "v2"):
        vals = out["score"][k]
        out["score"][k] = {"mean": round(statistics.mean(vals), 1) if vals else None,
                           "n": len(vals)}
    out["seconds"] = {"p50": round(statistics.median(out["seconds"]), 1) if out["seconds"] else None,
                      "p95": round(sorted(out["seconds"])[int(0.95 * (len(out["seconds"]) - 1))], 1)
                      if out["seconds"] else None}
    out["cost_per_filled_key_fact"] = (round(out["cost_usd"] / out["key_filled"], 5)
                                       if out["key_filled"] else None)
    out["gains"], out["losses"] = dict(out["gains"]), dict(out["losses"])
    out["evidence"] = dict(out["evidence"])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--json", help="also write the report here")
    args = ap.parse_args(argv)
    rows = nq(QUERY, {"limit": args.limit})
    rep = report([dict(r) if isinstance(r, dict) else r for r in rows])
    print(json.dumps(rep, indent=1))
    if args.json:
        open(args.json, "w", encoding="utf-8").write(json.dumps(rep, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
