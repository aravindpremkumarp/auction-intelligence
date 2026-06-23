"""
scripts/dedupe_documents.py
---------------------------
One-time cleanup for issue #45: collapse duplicate :Document nodes that
share the same filename under one :AuctionProperty into a single canonical
node.

For every (auction_id, filename) group with more than one :Document:

1. Pick the canonical node, preferring:
     - has public_url
     - has storage_key
     - most recent uploaded_at
     - most recent extracted_at
     - lowest internal id (stable tiebreak)
2. Copy any non-null property from the duplicates onto the canonical node
   only when the canonical's value is missing — never lose extracted_json
   or other audit fields.
3. Re-point any [:HAS_DOCUMENT] relationships from the duplicates onto the
   canonical node (skipping ones that already exist).
4. DETACH DELETE the duplicates.

With ``--global`` the grouping drops auction_id and a single canonical
absorbs every duplicate of the same filename across all auctions. This
collapses cross-auction copies of the same multi-property notice file
(e.g. a bank PDF announcing 48 lots) into one Document with N back-links.

Idempotent: safe to re-run. With ``--dry-run`` the script prints planned
actions without mutating the graph.

Run standalone:
    python -m scripts.dedupe_documents --dry-run
    python -m scripts.dedupe_documents
    python -m scripts.dedupe_documents --auction-id AUC123
    python -m scripts.dedupe_documents --global --dry-run
    python -m scripts.dedupe_documents --global
"""
from __future__ import annotations

import argparse
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.neo4j_client import run_query


# Group duplicates and pre-rank candidates inside Cypher so the Python side
# only has to drive the merge. The ranking key mirrors the docstring:
# ``public_url`` first, then ``storage_key``, then timestamps, then id.
FIND_DUPLICATES_CYPHER = """
MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
WHERE ($auction_id IS NULL OR a.auction_id = $auction_id)
  AND d.filename IS NOT NULL
WITH a, d.filename AS filename, collect(d) AS docs
WHERE size(docs) > 1
WITH a, filename, [x IN docs |
  {
    id:           elementId(x),
    storage_key:  x.storage_key,
    has_url:      CASE WHEN x.public_url  IS NULL THEN 0 ELSE 1 END,
    has_key:      CASE WHEN x.storage_key IS NULL THEN 0 ELSE 1 END,
    uploaded_at:  toString(x.uploaded_at),
    extracted_at: toString(x.extracted_at)
  }
] AS ranked
RETURN a.auction_id AS auction_id,
       filename     AS filename,
       ranked       AS candidates
ORDER BY a.auction_id, filename
"""


# Cross-auction grouping for ``--global`` mode: collapse copies of the same
# filename across all auctions into a single canonical node. The ranking
# fields are identical to the per-auction query so _pick_canonical works
# unchanged.
FIND_DUPLICATES_GLOBAL_CYPHER = """
MATCH (d:Document)
WHERE d.filename IS NOT NULL
WITH d.filename AS filename, collect(d) AS docs
WHERE size(docs) > 1
WITH filename, [x IN docs |
  {
    id:           elementId(x),
    storage_key:  x.storage_key,
    has_url:      CASE WHEN x.public_url  IS NULL THEN 0 ELSE 1 END,
    has_key:      CASE WHEN x.storage_key IS NULL THEN 0 ELSE 1 END,
    uploaded_at:  toString(x.uploaded_at),
    extracted_at: toString(x.extracted_at)
  }
] AS ranked
RETURN filename AS filename,
       ranked   AS candidates
ORDER BY filename
"""


# The canonical node receives any non-null property from a duplicate that it
# doesn't already have. Then we re-point every [:HAS_DOCUMENT] from the
# duplicates onto the canonical node (using MERGE so we don't introduce a
# parallel relationship). Finally the duplicates are DETACH DELETEd.
MERGE_DUPLICATES_CYPHER = """
MATCH (canonical:Document) WHERE elementId(canonical) = $canonical_id
WITH canonical
UNWIND $duplicate_ids AS dup_id
MATCH (dup:Document) WHERE elementId(dup) = dup_id

// Copy missing properties forward (canonical wins where it already has a value).
SET canonical.file_path                  = coalesce(canonical.file_path,                  dup.file_path),
    canonical.storage_key                = coalesce(canonical.storage_key,                dup.storage_key),
    canonical.public_url                 = coalesce(canonical.public_url,                 dup.public_url),
    canonical.content_type               = coalesce(canonical.content_type,               dup.content_type),
    canonical.doc_type                   = coalesce(canonical.doc_type,                   dup.doc_type),
    canonical.extracted_json             = coalesce(canonical.extracted_json,             dup.extracted_json),
    canonical.extracted_at               = coalesce(canonical.extracted_at,               dup.extracted_at),
    canonical.model                      = coalesce(canonical.model,                      dup.model),
    canonical.uploaded_at                = coalesce(canonical.uploaded_at,                dup.uploaded_at),
    canonical.markdown                   = coalesce(canonical.markdown,                   dup.markdown),
    canonical.notice_type                = coalesce(canonical.notice_type,                dup.notice_type),
    canonical.notice_type_classifier_pred = coalesce(canonical.notice_type_classifier_pred, dup.notice_type_classifier_pred),
    canonical.notice_type_confidence     = coalesce(canonical.notice_type_confidence,     dup.notice_type_confidence),
    canonical.notice_type_reasoning      = coalesce(canonical.notice_type_reasoning,      dup.notice_type_reasoning),
    canonical.notice_type_model          = coalesce(canonical.notice_type_model,          dup.notice_type_model),
    canonical.notice_type_classified_at  = coalesce(canonical.notice_type_classified_at,  dup.notice_type_classified_at),
    canonical.notice_type_verified_at    = coalesce(canonical.notice_type_verified_at,    dup.notice_type_verified_at),
    canonical.notice_type_overridden     = coalesce(canonical.notice_type_overridden,     dup.notice_type_overridden),
    canonical.property_count             = coalesce(canonical.property_count,             dup.property_count)

// Re-point any HAS_DOCUMENT relationship pointing at the duplicate.
WITH canonical, dup
OPTIONAL MATCH (a:AuctionProperty)-[r:HAS_DOCUMENT]->(dup)
WITH canonical, dup, collect(DISTINCT a) AS owners
FOREACH (a IN owners | MERGE (a)-[:HAS_DOCUMENT]->(canonical))

WITH dup
DETACH DELETE dup
"""


@lru_cache(maxsize=4096)
def _r2_object_exists(storage_key: str | None) -> bool:
    """True iff ``storage_key``'s object is actually present in R2.

    The whole point of dedupe is to leave one canonical Document per file, and
    that survivor's ``public_url`` is what the UI links to. Preferring a node
    whose object really exists stops us from electing a canonical whose URL
    dangles (the multi-property-notice 404 class) when a sibling has the file.

    Best-effort: any R2 error — including R2 not being configured in the
    runner — returns False so ranking falls back to the metadata-only signals.
    Dedupe must never hard-fail just because it couldn't reach R2. Cached so a
    filename shared across many auctions costs at most one HEAD per key.
    """
    if not storage_key:
        return False
    try:
        from pipeline import storage
        return storage.exists(storage_key)
    except Exception:
        return False


def _pick_canonical(candidates: list[dict]) -> tuple[str, list[str]]:
    """Return (canonical_id, [duplicate_ids]).

    Sort descending so the best candidate ends up at index 0. The first key is
    whether the node's R2 object actually exists (a node backed by a real file
    wins over one whose URL dangles). Then has_url and has_key are 1/0 ints (a
    node with public_url wins over one without); uploaded_at and extracted_at
    are ISO strings so plain string comparison gives newest-first under
    reverse=True.
    """
    def score(c: dict) -> tuple:
        return (
            1 if _r2_object_exists(c.get("storage_key")) else 0,
            int(c.get("has_url") or 0),
            int(c.get("has_key") or 0),
            c.get("uploaded_at") or "",
            c.get("extracted_at") or "",
        )

    ordered = sorted(candidates, key=score, reverse=True)
    canonical = ordered[0]
    duplicates = [c["id"] for c in ordered[1:]]
    return canonical["id"], duplicates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="List planned merges without modifying Neo4j.")
    parser.add_argument("--auction-id", default=None,
                        help="Limit cleanup to a single auction_id "
                             "(ignored with --global).")
    parser.add_argument("--global", dest="cross_auction", action="store_true",
                        help="Group by filename across all auctions; one "
                             "canonical Document absorbs every duplicate "
                             "of the same filename regardless of auction.")
    args = parser.parse_args()

    if args.cross_auction:
        groups = run_query(FIND_DUPLICATES_GLOBAL_CYPHER)
        group_label = "filename"
    else:
        groups = run_query(FIND_DUPLICATES_CYPHER, {"auction_id": args.auction_id})
        group_label = "(auction_id, filename)"

    if not groups:
        print("No duplicate documents found — nothing to do.")
        return 0

    print(f"Found {len(groups)} {group_label} groups with duplicates.")
    merged_groups = 0
    deleted_nodes = 0
    errors = 0

    for g in groups:
        canonical_id, duplicate_ids = _pick_canonical(g["candidates"])
        action = "[dry-run] would merge" if args.dry_run else "[merge]"
        if args.cross_auction:
            label = g["filename"]
        else:
            label = f"{g['auction_id']} :: {g['filename']}"
        print(
            f"  {action} {label} "
            f"keep={canonical_id[:24]}… drop={len(duplicate_ids)}"
        )

        if args.dry_run:
            continue

        try:
            run_query(MERGE_DUPLICATES_CYPHER, {
                "canonical_id":  canonical_id,
                "duplicate_ids": duplicate_ids,
            })
            merged_groups += 1
            deleted_nodes += len(duplicate_ids)
        except Exception as e:
            errors += 1
            print(f"    [error] {e}")

    print()
    print("=" * 50)
    if args.dry_run:
        print(f"  Dry-run only — graph not modified.")
        print(f"  Groups that would be merged: {len(groups)}")
    else:
        print(f"  Groups merged   : {merged_groups}")
        print(f"  Nodes deleted   : {deleted_nodes}")
        print(f"  Errors          : {errors}")
    print("=" * 50)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
