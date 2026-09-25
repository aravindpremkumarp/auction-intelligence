"""Renumber stored extractions' lots in the notice's own order (pipeline/lot_order).

Reports by default; ``--apply --only F`` writes one checked notice. Positions
come from the stored read, and a messy read gives messy positions, so every
suggestion is for a person to confirm against the notice first. For each multi-lot notice it renumbers
the extraction and the corrections that name lots by number (reviewer-added
entities, "not in the notice" / "not found" marks) in one write.

Skipped, and listed:
* a notice whose text changed since its stored read — its spans no longer
  point into the text, so positions mean nothing; it needs a full re-read;
* a notice with a person's lot-match decision on one of its listings — that
  decision names a lot by number, and moving it silently is exactly what this
  must not do. Renumber those by hand (or re-decide the match) if needed;
* a notice ``plan`` refuses: a lot it cannot place, or lots that run
  4, 5, 6, 1, 2, 3 in the text (pages joined in the wrong order).

Writing changes the extraction only. Rebuild lots and listings afterwards:
    python -m pipeline.promote_extractions --filename F --rebuild-lots --skip-parcels
    python -m pipeline.apply_extractions --filename F

Run:
    python -m scripts.renumber_lots                    # report every notice (writes nothing)
    python -m scripts.renumber_lots --only F --apply
"""
from __future__ import annotations

import argparse
import json
import sys

from api.neo4j_client import run_query, run_read_query
from pipeline.lot_order import apply, plan


def human_decided_files() -> set[str]:
    """Notices with a person's lot-match decision on one of their listings."""
    rows = run_read_query(
        "MATCH (r:ResolutionDecision {kind: 'lot-match'}) "
        "WHERE r.decided_by IS NOT NULL AND NOT r.decided_by STARTS WITH 'system:' "
        "RETURN r.payload_json AS p", max_rows=20_000, timeout=60.0)
    out = set()
    for r in rows:
        try:
            key = json.loads(r["p"] or "{}").get("lot_key") or ""
        except (TypeError, ValueError):
            continue
        if "#" in key:
            out.add(key.rsplit("#", 1)[0])
    return out


def load(only: list[str]) -> list[dict]:
    where = "AND d.filename IN $only " if only else ""
    return run_read_query(
        "MATCH (d:Document) WHERE d.stitched_into IS NULL "
        "  AND d.extraction_json IS NOT NULL " + where +
        "RETURN d.filename AS f, d.extraction_json AS j, "
        "       coalesce(d.extraction_corrections_json, '{}') AS c, "
        "       coalesce(d.stitched_markdown, d.markdown) AS md, "
        "       coalesce(d.stitched_expected_lot_count, d.expected_lot_count) AS n, "
        "       toString(d.extraction_at) AS xa, "
        "       toString(d.markdown_loaded_at) AS ma, toString(d.stitched_at) AS sa",
        {"only": only}, max_rows=20_000, timeout=600.0)


def _text_changed(r: dict) -> bool:
    from scripts.reset_langextract_and_extract import _when
    read_at = _when(r.get("xa"))
    text_at = max((t for t in (_when(r.get("ma")), _when(r.get("sa"))) if t),
                  default=None)
    return bool(read_at and text_at and text_at > read_at)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", action="append", default=[], metavar="FILENAME")
    ap.add_argument("--apply", action="store_true", help="write the new numbers")
    args = ap.parse_args(argv)
    if args.apply and not args.only:
        # Positions come from the reads themselves; a person checks each
        # suggestion against the notice before it is written.
        print("--apply needs --only: check each suggestion against the notice first")
        return 2

    human = human_decided_files()
    todo, skipped, written = [], [], []
    for r in load(args.only):
        try:
            ents = json.loads(r["j"] or "[]")
            corr = json.loads(r["c"] or "{}")
        except (TypeError, ValueError):
            continue
        mapping, why = plan(ents, r["md"], r["n"])
        if not mapping:
            if why not in ("single lot", "already in order"):
                skipped.append((r["f"], why))
            continue
        if _text_changed(r):
            skipped.append((r["f"], "text changed since the stored read — needs a full re-read"))
            continue
        if r["f"] in human:
            skipped.append((r["f"], "a person matched a listing to one of its lots"))
            continue
        moves = ", ".join(f"{o}→{n}" for o, n in
                          sorted(mapping.items(), key=lambda kv: (len(kv[0]), kv[0])))
        todo.append(r["f"])
        print(f"  {r['f']}: {why}: {moves}", flush=True)
        if args.apply:
            new_ents, new_corr = apply(ents, corr, mapping)
            run_query(
                "MATCH (d:Document {filename: $f}) "
                "SET d.extraction_json = $j, d.extraction_corrections_json = $c",
                {"f": r["f"], "j": json.dumps(new_ents, ensure_ascii=False),
                 "c": json.dumps(new_corr, ensure_ascii=False)})
            written.append(r["f"])
    for f, why in skipped:
        print(f"  [skip] {f}: {why}")
    print(f"{len(todo)} to renumber, {len(skipped)} skipped"
          + (f"; wrote {len(written)}" if args.apply else "; report only"))
    if written:
        from pipeline.key_entities import stamp_key_scores
        stamp_key_scores(written)
        print("rebuild lots for: " + " ".join(written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
