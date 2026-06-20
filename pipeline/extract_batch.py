"""Chunked corpus extraction with per-batch quality feedback.

Processes notices in batches of BATCH_SIZE (default 30). For each batch it writes
the raw results, then an error-pattern REPORT — issue-code frequencies, field
coverage %, score distribution, and a review queue of the worst docs. That report
is the "feedback from the last 30 runs": read it, fix the top recurring pattern in
the prompt/examples, re-gate with `python -m evals.langextract_eval`, then continue.

Markdown source (in priority order):
  --dir <path>   : read every <aid>.txt in a directory (offline, no DB).
  --neo4j        : pull Document.markdown from Neo4j (needs api.neo4j_client + creds).

Outputs under pipeline/output/batches/<run>/:
  batch_NN.results.jsonl   one line per doc (aid, score, issues, fields, stats)
  batch_NN.report.json     aggregated feedback for the batch
  review_queue.jsonl       low-scoring docs across all batches (for human review)
  summary.json             run-wide rollup

Run:  python -m pipeline.extract_batch --dir evals/fixtures --batch-size 3
"""
from __future__ import annotations

import argparse
import collections
import json
from datetime import datetime, timezone
from pathlib import Path

from pipeline import langextract_examples as LX
from pipeline.validators import COVERAGE_FIELDS, validate

OUT_ROOT = Path(__file__).resolve().parent / "output" / "batches"
REVIEW_SCORE_THRESHOLD = 80   # docs at/under this get queued for human review


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def load_from_dir(path: str) -> list[tuple[str, str]]:
    d = Path(path)
    return [(p.stem, p.read_text(encoding="utf-8"))
            for p in sorted(d.glob("*.txt"))]


def load_from_neo4j(limit: int | None) -> list[tuple[str, str]]:
    from api.neo4j_client import run_read_query  # noqa: import here (needs creds)
    rows = run_read_query(
        "MATCH (p:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document) "
        "WHERE d.markdown IS NOT NULL AND d.markdown <> '' "
        "RETURN p.auction_id AS aid, d.markdown AS md ORDER BY aid"
        + (f" LIMIT {int(limit)}" if limit else ""),
        max_rows=20_000, timeout=60.0)
    return [(r["aid"], r["md"]) for r in rows]


def _extract_robust(md: str, tries: int = 3):
    res = None
    for _ in range(tries):
        res = LX.extract(md)
        if res.extractions:
            return res
    return res


def build_batch_report(records: list[dict]) -> dict:
    """Aggregate one batch's per-doc validations into actionable feedback."""
    n = len(records)
    issue_freq = collections.Counter()
    issue_examples: dict = collections.defaultdict(list)
    field_cov = collections.Counter()
    scores = []
    for r in records:
        scores.append(r["score"])
        for f in COVERAGE_FIELDS:
            if f in r["fields"]:
                field_cov[f] += 1
        for iss in r["issues"]:
            issue_freq[iss["code"]] += 1
            if len(issue_examples[iss["code"]]) < 5:
                issue_examples[iss["code"]].append(r["aid"])
    coverage = {f: round(field_cov[f] / n * 100, 1) for f in COVERAGE_FIELDS}
    # the improvement targets: low-coverage fields + most-frequent issues
    targets = sorted(coverage.items(), key=lambda kv: kv[1])[:6]
    return {
        "docs": n,
        "mean_score": round(sum(scores) / n, 1) if n else 0,
        "min_score": min(scores) if scores else 0,
        "issue_frequency": dict(issue_freq.most_common()),
        "issue_examples": {k: issue_examples[k] for k in issue_freq},
        "field_coverage_pct": coverage,
        "improvement_targets": {
            "lowest_coverage_fields": targets,
            "top_issues": issue_freq.most_common(5),
        },
    }


def run(docs: list[tuple[str, str]], batch_size: int) -> None:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = OUT_ROOT / run_id
    out.mkdir(parents=True, exist_ok=True)
    review = (out / "review_queue.jsonl").open("w", encoding="utf-8")
    all_scores, all_issue_freq = [], collections.Counter()

    for bi, batch in enumerate(_chunks(docs, batch_size), start=1):
        records = []
        with (out / f"batch_{bi:02d}.results.jsonl").open("w", encoding="utf-8") as rf:
            for aid, md in batch:
                res = _extract_robust(md)
                v = validate(res.extractions, source_text=md)
                rec = {"aid": aid, "score": v["score"], "issues": v["issues"],
                       "fields": v["fields"], "stats": v["stats"]}
                records.append(rec)
                rf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if v["score"] <= REVIEW_SCORE_THRESHOLD:
                    review.write(json.dumps(
                        {"aid": aid, "score": v["score"],
                         "issues": [i["code"] for i in v["issues"]]},
                        ensure_ascii=False) + "\n")
        report = build_batch_report(records)
        (out / f"batch_{bi:02d}.report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        all_scores += [r["score"] for r in records]
        for r in records:
            for iss in r["issues"]:
                all_issue_freq[iss["code"]] += 1
        print(f"batch {bi:02d}: {len(records)} docs  mean_score={report['mean_score']}  "
              f"top_issues={report['improvement_targets']['top_issues'][:3]}")

    review.close()
    summary = {
        "run_id": run_id, "total_docs": len(docs), "batch_size": batch_size,
        "mean_score": round(sum(all_scores) / len(all_scores), 1) if all_scores else 0,
        "issue_frequency": dict(all_issue_freq.most_common()),
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDONE — {len(docs)} docs, mean_score={summary['mean_score']}")
    print(f"Reports: {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dir", help="directory of <aid>.txt markdown files")
    src.add_argument("--neo4j", action="store_true", help="pull markdown from Neo4j")
    ap.add_argument("--batch-size", type=int, default=30)
    ap.add_argument("--limit", type=int, default=None, help="cap docs (neo4j)")
    args = ap.parse_args()
    docs = load_from_dir(args.dir) if args.dir else load_from_neo4j(args.limit)
    if not docs:
        print("no documents found")
        return 1
    run(docs, args.batch_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
