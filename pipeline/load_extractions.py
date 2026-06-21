"""Populate Document.extraction_json for the /review/extraction review surface.

Runs the canonical LangExtract pipeline (pipeline/langextract_examples.extract)
over each Document's markdown and writes the grounded entities back to Neo4j as a
compact JSON array — the shape api/review/extraction.py reads:

  extraction_json = [{id, cls, text, start, end, attrs}, ...]

Idempotent-ish: by default skips Documents that already have extraction_json
(use --force to re-extract). Review state (corrections / verified) is preserved.

Run:  NEO4J_HTTP_API=1 LANGEXTRACT_PROVIDER=openrouter \
        python -m pipeline.load_extractions --limit 50
"""
from __future__ import annotations

import argparse
import json

from api.neo4j_client import run_query, run_read_query
from pipeline import langextract_examples as LX


def _fetch(limit: int | None, force: bool, filename: str | None) -> list[dict]:
    where = "d.markdown IS NOT NULL AND d.markdown <> ''"
    if not force:
        where += " AND d.extraction_json IS NULL"
    if filename:
        where += " AND d.filename = $fn"
    return run_read_query(
        f"MATCH (d:Document) WHERE {where} "
        "RETURN d.filename AS filename, d.markdown AS md ORDER BY d.filename"
        + (f" LIMIT {int(limit)}" if limit else ""),
        {"fn": filename} if filename else None,
        max_rows=20_000, timeout=120.0)


def _entities(res) -> list[dict]:
    out = []
    for i, e in enumerate(res.extractions):
        ci = getattr(e, "char_interval", None)
        out.append({
            "id": str(i),
            "cls": e.extraction_class,
            "text": e.extraction_text,
            "start": getattr(ci, "start_pos", None) if ci else None,
            "end": getattr(ci, "end_pos", None) if ci else None,
            "attrs": e.attributes or {},
        })
    return out


def run(limit: int | None, force: bool, filename: str | None) -> int:
    docs = _fetch(limit, force, filename)
    print(f"to extract: {len(docs)} document(s)")
    ok = fail = 0
    for d in docs:
        fn = d["filename"]
        try:
            res = LX.extract(d["md"])
        except Exception as e:  # keep going; one bad doc shouldn't stop the load
            fail += 1
            print(f"  [fail] {fn}: {e}")
            continue
        ents = _entities(res)
        run_query(
            """
            MATCH (d:Document {filename:$fn})
            SET d.extraction_json = $j,
                d.extraction_review_status =
                    coalesce(d.extraction_review_status, 'pending')
            RETURN d.filename
            """,
            {"fn": fn, "j": json.dumps(ents, ensure_ascii=False)})
        ok += 1
        print(f"  [{ok}] {fn}: {len(ents)} fields")
    print(f"done — wrote {ok}, failed {fail}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true", help="re-extract even if present")
    ap.add_argument("--filename", help="only this Document.filename")
    args = ap.parse_args()
    return run(args.limit, args.force, args.filename)


if __name__ == "__main__":
    raise SystemExit(main())
