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
from pipeline.validators import normalize_identifier_kind

# pipeline.langextract_examples is imported lazily inside run() — it pulls the
# heavy `langextract` dependency, which isn't needed to import this module for
# the batch-numbering helper (or its unit tests).


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
        attrs = dict(e.attributes or {})
        # Safety net: models sometimes copy the document's label as the
        # identifier kind ("T.S.No") instead of the enum — normalize, keeping
        # the original in kind_raw so nothing is lost for review.
        if e.extraction_class == "identifier" and attrs.get("kind"):
            kind, changed = normalize_identifier_kind(attrs["kind"])
            if changed:
                attrs["kind_raw"] = attrs["kind"]
                attrs["kind"] = kind
        out.append({
            "id": str(i),
            "cls": e.extraction_class,
            "text": e.extraction_text,
            "start": getattr(ci, "start_pos", None) if ci else None,
            "end": getattr(ci, "end_pos", None) if ci else None,
            "attrs": attrs,
        })
    return out


def _next_batch() -> int:
    """Next global extraction batch number (B1, B2, …). Every document written by
    a single `run()` shares this number, so the review queue can tag a run and the
    reviewer can tell at a glance which notices came from the latest re-extraction."""
    rows = run_read_query(
        "MATCH (d:Document) WHERE d.extraction_batch IS NOT NULL "
        "RETURN max(d.extraction_batch) AS m LIMIT 1")
    cur = rows[0]["m"] if rows else None
    return int(cur) + 1 if cur is not None else 1


def run(limit: int | None, force: bool, filename: str | None) -> int:
    from pipeline import langextract_examples as LX
    docs = _fetch(limit, force, filename)
    print(f"to extract: {len(docs)} document(s)")
    if not docs:
        print("done — wrote 0, failed 0")
        return 0
    batch = _next_batch()
    print(f"batch B{batch}")
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
                d.extraction_at    = datetime(),
                d.extraction_batch = $batch,
                d.extraction_review_status =
                    coalesce(d.extraction_review_status, 'pending')
            RETURN d.filename
            """,
            {"fn": fn, "j": json.dumps(ents, ensure_ascii=False), "batch": batch})
        ok += 1
        print(f"  [{ok}] {fn}: {len(ents)} fields")
    print(f"done — wrote {ok}, failed {fail} (batch B{batch})")
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
