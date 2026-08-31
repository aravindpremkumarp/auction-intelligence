"""Remove the retired `AuctionProperty.resolved_lot_key` property.

Phase 4 of the lot-key migration. The string is `"<filename>#<lot_index>"`,
and lot_index is the extraction model's own numbering: re-extract a notice and
a key saying `#3` still RESOLVES — to a different property. Nothing raises.
Every reader and writer now uses `(:AuctionProperty)-[:IS_LOT]->(:Lot)`
instead, so the property is dead weight that can only ever drift.

REFUSES TO RUN while the two disagree. A listing holding a key but no edge
would lose its resolution outright, and one whose edge names a different lot
means something upstream is still writing the string. Either way the answer is
to fix that first, not to delete the evidence.

`--out` writes every (auction_id, key) pair it is about to remove, so the
property can be restored if some reader nobody knew about turns up.

Usage:
    NEO4J_HTTP_API=1 python -m scripts.drop_resolved_lot_key --dry-run
    NEO4J_HTTP_API=1 python -m scripts.drop_resolved_lot_key --out dropped.json
"""
from __future__ import annotations

import argparse
import json
import sys

from api.neo4j_client import run_query, run_read_query

_AGREEMENT = """
MATCH (a:AuctionProperty)
OPTIONAL MATCH (a)-[:IS_LOT]->(l:Lot)
WITH a.resolved_lot_key AS key, l.lot_key AS edge
RETURN count(*) AS listings,
       sum(CASE WHEN key IS NOT NULL THEN 1 ELSE 0 END) AS with_key,
       sum(CASE WHEN edge IS NOT NULL THEN 1 ELSE 0 END) AS with_edge,
       sum(CASE WHEN key IS NOT NULL AND edge IS NULL THEN 1 ELSE 0 END)
         AS key_without_edge,
       sum(CASE WHEN key IS NOT NULL AND edge IS NOT NULL AND key <> edge
                THEN 1 ELSE 0 END) AS disagree
"""

_CAPTURE = """
MATCH (a:AuctionProperty) WHERE a.resolved_lot_key IS NOT NULL
RETURN a.auction_id AS auction_id, a.resolved_lot_key AS resolved_lot_key
"""

_DROP = """
MATCH (a:AuctionProperty) WHERE a.resolved_lot_key IS NOT NULL
WITH a LIMIT $limit
REMOVE a.resolved_lot_key, a.lot_resolved_at
RETURN count(a) AS dropped
"""

_BATCH = 1000


def run(dry_run: bool, out_path: str | None) -> int:
    row = (run_read_query(_AGREEMENT, timeout=120.0) or [{}])[0]
    print(f"listings: {row.get('listings')}   with key: {row.get('with_key')}   "
          f"with edge: {row.get('with_edge')}", flush=True)

    blockers = []
    if int(row.get("key_without_edge") or 0):
        blockers.append(
            f"{row['key_without_edge']} listing(s) hold a key but NO edge — "
            f"dropping the key would lose their resolution outright")
    if int(row.get("disagree") or 0):
        blockers.append(
            f"{row['disagree']} listing(s) have an edge naming a DIFFERENT lot "
            f"than their key — something is still writing the string")
    if blockers:
        for b in blockers:
            print(f"  BLOCKED: {b}", file=sys.stderr)
        print("  run pipeline.apply_extractions first, then re-check",
              file=sys.stderr)
        return 1

    if dry_run:
        print(f"  [dry-run] would remove the property from "
              f"{row.get('with_key')} listing(s)", flush=True)
        return 0

    if out_path:
        rows = run_read_query(_CAPTURE, max_rows=200_000, timeout=180.0)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump([dict(r) for r in rows], fh)
        print(f"  captured {len(rows)} key(s) to {out_path}", flush=True)
    elif int(row.get("with_key") or 0):
        print("  refusing to drop without --out: pass a path so the removal "
              "is reversible", file=sys.stderr)
        return 1

    total = 0
    while True:
        out = run_query(_DROP, {"limit": _BATCH})
        n = int((list(out[0].values())[0] if out else 0) or 0)
        total += n
        if n < _BATCH:
            break
    print(f"  removed the property from {total} listing(s)", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", help="write the removed keys here first")
    args = ap.parse_args()
    return run(args.dry_run, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
