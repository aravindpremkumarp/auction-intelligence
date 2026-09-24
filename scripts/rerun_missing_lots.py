"""Re-extract notices that came back short of lots — without losing any.

A plain re-extraction (scripts/reset_langextract_and_extract.py, the review
page's ▶ Re-run) REPLACES a notice's entities. On a long multi-lot notice the
model often stops early, so a second run can return fewer lots than the first
and the lots the first run did read are gone. This script only ever adds:

1. **Backup.** The current entities, corrections, score and batch are copied to
   ``extraction_json_prev`` / ``extraction_corrections_json_prev`` /
   ``extraction_score_prev`` / ``extraction_batch_prev`` before anything is
   written, so every notice can be put back by hand.
2. **Re-extract** with the canonical path (per-notice-type model routing, the
   reviewer's lot count and the portal roster in the prompt).
3. **Merge.** The new run's lots are kept; every lot the OLD run had and the
   new one missed is carried over whole. Notice-level entities (creditor,
   contact, EMD account, terms, extras) are carried only for a class the new
   run has none of. Carried spans are re-anchored against the current
   markdown, and carried entities get a ``p`` id prefix so they can never
   collide with the new run's ids.
4. **Keep only a gain.** The merged result is written only when it has MORE
   lots than the stored one; otherwise the notice is left exactly as it was.

Reviewer input survives: ``add:*`` entities and ``absent:*`` marks are kept
as they are (both are keyed by lot, not by entity id), and a text correction
on an entity that was carried moves with it to the ``p`` id. A correction on
an entity the new run replaced has nothing left to apply to; it stays in
``extraction_corrections_json_prev``.

Targets: every notice with fewer extracted lots than the reviewer counted at
classification (the review queue's "missing lots" filter), or --filename.

Run:
    NEO4J_HTTP_API=1 LANGEXTRACT_PROVIDER=openrouter \\
        python -m scripts.rerun_missing_lots --dry-run      # extract + report, no writes
    NEO4J_HTTP_API=1 LANGEXTRACT_PROVIDER=openrouter \\
        python -m scripts.rerun_missing_lots
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from api.neo4j_client import run_query, run_read_query
from api.review.extraction import extraction_stale
from api.review.grounding import reanchor
from pipeline.extract_routing import passes_for, select_extract_model
from pipeline.key_entities import extracted_lot_count, stamp_key_scores
from pipeline.load_extractions import ROSTER_CYPHER, _entities, _next_batch
from pipeline.validators import SCORE_VERSION, validate_stored

# Classes that describe the notice, not one lot (pipeline/key_entities.py).
NOTICE_LEVEL = frozenset({"secured_creditor", "contact", "emd_account",
                          "full_terms", "extras"})
CARRIED_PREFIX = "p"


def _lot(e: dict) -> str:
    return str((e.get("attrs") or {}).get("lot_index") or "1")


def merge_lots(old: list[dict], new: list[dict], markdown: str | None = None,
               markdown_changed: bool = False) -> tuple[list[dict], dict]:
    """New entities, plus the old run's lots the new run does not have.

    Returns ``(merged, id_map)`` where ``id_map`` maps each carried entity's
    old id to its new one, for moving reviewer corrections along."""
    new_lots = {_lot(e) for e in new if e.get("cls") not in NOTICE_LEVEL}
    new_classes = {e.get("cls") for e in new}
    carried = []
    for e in old:
        if not isinstance(e, dict):
            continue
        if e.get("cls") in NOTICE_LEVEL:
            keep = e.get("cls") not in new_classes
        else:
            keep = _lot(e) not in new_lots
        if keep:
            carried.append(e)
    # If the markdown was rewritten since the old run, re-find each span and
    # drop one that no longer lands rather than point it at the wrong text
    # (api/review/grounding.py — on unchanged text the model's span stands).
    anchored, _ = reanchor(carried, markdown, markdown_changed=markdown_changed)
    id_map, out = {}, list(new)
    for src, a in zip(carried, anchored):
        old_id = str(src.get("id"))
        new_id = CARRIED_PREFIX + old_id
        id_map[old_id] = new_id
        out.append({"id": new_id, "cls": src.get("cls"), "text": src.get("text"),
                    "start": a.get("start"), "end": a.get("end"),
                    "attrs": dict(src.get("attrs") or {})})
    return out, id_map


def carry_corrections(corrections: dict, id_map: dict) -> dict:
    """Corrections that still apply after a merge (see module docstring)."""
    out = {}
    for k, v in (corrections or {}).items():
        if k.startswith("add:") or k.startswith("absent:"):
            out[k] = v
        elif k in id_map:
            out[id_map[k]] = v
    return out


def select_targets(filenames: list[str] | None) -> list[dict]:
    where = ("d.extraction_json IS NOT NULL AND d.stitched_into IS NULL "
             "AND coalesce(d.stitched_markdown, d.markdown) <> '' ")
    if filenames:
        where += "AND d.filename IN $fns "
    else:
        where += ("AND d.extraction_lot_count < "
                  "coalesce(d.stitched_expected_lot_count, d.expected_lot_count) ")
    return run_read_query(
        f"MATCH (d:Document) WHERE {where} "
        + ROSTER_CYPHER +
        "RETURN d.filename AS filename, "
        "       coalesce(d.stitched_markdown, d.markdown) AS md, "
        "       d.notice_type AS notice_type, "
        "       coalesce(d.stitched_expected_lot_count, d.expected_lot_count) "
        "AS expected_lot_count, "
        "       roster AS roster, "
        "       d.extraction_json AS ej, "
        "       coalesce(d.extraction_corrections_json, '{}') AS cj, "
        "       toString(d.extraction_at) AS ex_at, "
        "       toString(d.markdown_reextracted_at) AS md_re_at, "
        "       toString(d.markdown_loaded_at) AS md_at, "
        "       toString(d.extraction_stale_at) AS stale_at "
        "ORDER BY d.filename",
        {"fns": filenames or []}, max_rows=1000, timeout=120.0)


def rerun_one(d: dict, batch: int, dry_run: bool) -> dict:
    from pipeline import langextract_examples as LX  # heavy import, defer
    fn, md = d["filename"], d["md"]
    old = json.loads(d["ej"] or "[]")
    before = extracted_lot_count(old) or 0
    model_id, reasoning_off = select_extract_model(d.get("notice_type"))
    res = LX.extract(md, model_id=model_id, reasoning_off=reasoning_off,
                     expected_lot_count=d.get("expected_lot_count"),
                     roster=d.get("roster"),
                     passes=passes_for(d.get("notice_type")))
    new = _entities(res, md)
    fresh = extracted_lot_count(new) or 0
    changed = extraction_stale(d.get("md_re_at"), d.get("md_at"),
                               d.get("ex_at"), d.get("stale_at"))
    merged, id_map = merge_lots(old, new, md, markdown_changed=changed)
    after = extracted_lot_count(merged) or 0
    out = {"filename": fn, "expected": d.get("expected_lot_count"),
           "before": before, "new_run": fresh, "merged": after,
           "carried": len(id_map), "written": False}
    if not new or after <= before or dry_run:
        return out
    corr = json.loads(d["cj"] or "{}")
    score = validate_stored(merged, source_text=md)["score"]
    rows = run_query(
        """
        MATCH (d:Document {filename: $fn})
        // Only if nobody re-extracted it while the model was running — the
        // backup and the merge are both built from the entities read above.
        WHERE d.extraction_json = $old_json
        SET d.extraction_json_prev             = d.extraction_json,
            d.extraction_corrections_json_prev = d.extraction_corrections_json,
            d.extraction_score_prev            = d.extraction_score,
            d.extraction_batch_prev            = d.extraction_batch,
            d.extraction_prev_saved_at         = datetime(),
            d.extraction_json             = $j,
            d.extraction_corrections_json = $c,
            d.extraction_score            = $score,
            d.extraction_score_version    = $score_version,
            d.extraction_at               = datetime(),
            d.extraction_batch            = $batch,
            d.extraction_merged_lots      = $carried_lots,
            // New entities nobody has read: same rule as every re-extraction.
            d.extraction_review_status    = 'pending',
            d.extraction_stale_at         = NULL
        REMOVE d.extraction_verified_by, d.extraction_verified_at
        RETURN d.filename AS filename
        """,
        {"fn": fn, "old_json": d["ej"],
         "j": json.dumps(merged, ensure_ascii=False),
         "c": json.dumps(carry_corrections(corr, id_map), ensure_ascii=False),
         "score": score, "score_version": SCORE_VERSION, "batch": batch,
         "carried_lots": sorted({_lot(e) for e in merged
                                 if str(e.get("id", "")).startswith(CARRIED_PREFIX)
                                 and e.get("cls") not in NOTICE_LEVEL})})
    if rows:
        stamp_key_scores([fn])
        out["written"] = True
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--filename", action="append",
                   help="re-run these notices instead of the missing-lots set")
    p.add_argument("--dry-run", action="store_true",
                   help="extract and report the merge, write nothing")
    p.add_argument("--concurrency", type=int, default=3)
    args = p.parse_args()

    docs = select_targets(args.filename)
    print(f"notices to re-run: {len(docs)}")
    if not docs:
        return 0
    batch = _next_batch()
    print(f"batch B{batch}{' (dry run)' if args.dry_run else ''}")
    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futs = {pool.submit(rerun_one, d, batch, args.dry_run): d["filename"]
                for d in docs}
        for f in as_completed(futs):
            try:
                r = f.result()
            except Exception as e:  # one notice failing keeps the rest going
                failed += 1
                print(f"  FAILED {futs[f]}: {type(e).__name__}: {e}")
                continue
            verdict = ("written" if r["written"] else
                       "kept old (no gain)" if r["merged"] <= r["before"] else
                       "dry run")
            print(f"  {r['filename'][:48]:48} lots {r['before']} → {r['merged']}"
                  f" of {r['expected']} (new run {r['new_run']},"
                  f" carried {r['carried']} entities) — {verdict}")
    if failed:
        print(f"{failed} notice(s) failed; they keep their old extraction.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
