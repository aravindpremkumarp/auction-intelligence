"""Chunked corpus extraction with per-batch quality feedback.

Processes notices in batches of BATCH_SIZE (default 30). For each batch it writes
the raw results, then an error-pattern REPORT — issue-code frequencies, field
coverage %, score distribution, and a review queue of the worst docs. That report
is the "feedback from the last 30 runs": read it, fix the top recurring pattern in
the prompt/examples, re-gate with `python -m evals.langextract_eval`, then continue.

Source:
  --dir <path>   : read every <aid>.txt in a directory and extract (offline).
  --neo4j        : pull Document.markdown from Neo4j and extract.
  --from-graph   : report from already-loaded Document.extraction_json
                   (pipeline/load_extractions.py) — validators only, NO LLM cost.
                   This is the consolidated path: extract once via the loader,
                   then regenerate feedback reports for free as often as needed.

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
from pipeline.langextract_run import USAGE, install_usage_tracking
from pipeline.validators import COVERAGE_FIELDS, validate, validate_stored

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
    """One row per DISTINCT Document with markdown, deduped by content.

    A notice/Document can back several AuctionProperties (and re-auctions reuse
    identical markdown under different ids), so extracting per property-document
    pair would re-run — and re-bill — the same notice many times. We key by a
    representative auction_id and drop byte-identical markdown.
    """
    from api.neo4j_client import run_read_query  # needs creds (NEO4J_HTTP_API=1 here)
    rows = run_read_query(
        "MATCH (d:Document) WHERE d.markdown IS NOT NULL AND d.markdown <> '' "
        "OPTIONAL MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d) "
        "WITH d, collect(a.auction_id) AS aids "
        "RETURN coalesce(aids[0], d.storage_key, d.file_path) AS aid, "
        "       d.markdown AS md ORDER BY aid"
        + (f" LIMIT {int(limit)}" if limit else ""),
        max_rows=20_000, timeout=120.0)
    seen: set = set()
    out: list[tuple[str, str]] = []
    for r in rows:
        h = hash(r["md"])
        if h in seen:
            continue
        seen.add(h)
        out.append((r["aid"], r["md"]))
    return out


def _extract_robust(md: str, tries: int = 3):
    res = None
    for _ in range(tries):
        res = LX.extract(md)
        if res.extractions:
            return res
    return res


def load_from_graph(limit: int | None) -> list[tuple]:
    """Read already-extracted entities from Document.extraction_json (populated by
    pipeline/load_extractions.py). The reporting/feedback pass runs off this — no
    re-extraction, no LLM cost — so extraction happens exactly once per notice."""
    from api.neo4j_client import run_read_query
    rows = run_read_query(
        "MATCH (d:Document) WHERE d.extraction_json IS NOT NULL "
        "OPTIONAL MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d) "
        "WITH d, collect(a.auction_id) AS aids "
        "RETURN coalesce(aids[0], d.storage_key, d.filename) AS aid, "
        "       d.markdown AS md, d.extraction_json AS ej ORDER BY aid"
        + (f" LIMIT {int(limit)}" if limit else ""),
        max_rows=20_000, timeout=120.0)
    out: list[tuple] = []
    for r in rows:
        try:
            ents = json.loads(r["ej"] or "[]")
        except json.JSONDecodeError:
            ents = []
        out.append((r["aid"], r["md"] or "", ents))
    return out


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


def run(docs: list, batch_size: int, from_graph: bool = False) -> None:
    install_usage_tracking()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = OUT_ROOT / run_id
    out.mkdir(parents=True, exist_ok=True)
    review = (out / "review_queue.jsonl").open("w", encoding="utf-8")
    all_scores, all_issue_freq = [], collections.Counter()

    for bi, batch in enumerate(_chunks(docs, batch_size), start=1):
        records = []
        with (out / f"batch_{bi:02d}.results.jsonl").open("w", encoding="utf-8") as rf:
            for item in batch:
                if from_graph:
                    # Already-extracted entities from Document.extraction_json —
                    # validate (pure, no LLM) instead of re-extracting.
                    aid, md, entities = item
                    v = validate_stored(entities, source_text=md)
                else:
                    aid, md = item
                    res = _extract_robust(md)
                    v = validate(res.extractions, source_text=md)
                rec = {"aid": aid, "score": v["score"], "issues": v["issues"],
                       "fields": v["fields"], "stats": v["stats"]}
                records.append(rec)
                USAGE.docs += 1
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
        "usage": {"llm_calls": USAGE.calls, "input_tokens": USAGE.prompt_tokens,
                  "cached_tokens": USAGE.cached_tokens,
                  "output_tokens": USAGE.output_tokens,
                  "est_cost_usd": round(USAGE.est_cost, 4)},
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDONE — {len(docs)} docs, mean_score={summary['mean_score']}")
    print("\n=== USAGE ===\n" + USAGE.report())
    print(f"Reports: {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dir", help="directory of <aid>.txt markdown files (extract)")
    src.add_argument("--neo4j", action="store_true",
                     help="pull markdown from Neo4j and extract")
    src.add_argument("--from-graph", action="store_true",
                     help="report from already-loaded Document.extraction_json "
                          "(no re-extraction / no LLM cost)")
    ap.add_argument("--batch-size", type=int, default=30)
    ap.add_argument("--limit", type=int, default=None, help="cap docs")
    args = ap.parse_args()
    if args.from_graph:
        docs = load_from_graph(args.limit)
    elif args.dir:
        docs = load_from_dir(args.dir)
    else:
        docs = load_from_neo4j(args.limit)
    if not docs:
        print("no documents found")
        return 1
    run(docs, args.batch_size, from_graph=args.from_graph)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
