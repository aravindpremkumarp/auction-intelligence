"""Snapshot every :Lot and its owned children, so a rebuild can be undone.

Phase 3 of the lot-key migration makes lots DISPOSABLE — promote deletes a
document's lots and rebuilds them from the current extraction. That destroys
the four child kinds a lot owns outright (Measurement, Boundary, Schedule,
Auction) and, unlike everything else in the pipeline, it is not recoverable by
re-running: the previous extraction that produced them is gone.

So this exists to be run BEFORE the first rebuild, not after something breaks.

What it captures
----------------
Per lot: its own properties, and every owned child with the relationship that
attached it. Shared nodes (Identifier, Borrower, Parcel) are recorded by KEY
only, never by content — a rebuild detaches those and never deletes them, so
restoring their content would overwrite notices this run never touched.

Usage:
    NEO4J_HTTP_API=1 python -m scripts.snapshot_lots --out lots.json.gz
    NEO4J_HTTP_API=1 python -m scripts.snapshot_lots --out l.json.gz --filename F
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import datetime, timezone

from api.neo4j_client import run_read_query

#: Children a lot owns outright — one lot each, verified by
#: scripts/audit_lot_links.py section E against the live graph. These die with
#: the lot, so their content is what has to be captured.
OWNED = (("Measurement", "HAS_EXTENT"), ("Boundary", "HAS_BOUNDARY"),
         ("Schedule", "HAS_SCHEDULE"), ("Auction", "OFFERED_IN"))

#: Shared across lots — one Identifier is cited by 175 of them. A rebuild
#: detaches these and never deletes them, so only the reference is recorded.
SHARED = (("Identifier", "MENTIONS_IDENTIFIER", "value_norm"),
          ("Borrower", "HAS_PARTY", "name"),
          ("Parcel", "IS_PARCEL", "parcel_id"))

_PAGE = 400


def _lots(filename: str | None) -> list[dict]:
    where = "WHERE d.filename = $filename" if filename else ""
    rows, skip = [], 0
    while True:
        page = run_read_query(
            f"""
            MATCH (d:Document)-[:HAS_LOT]->(l:Lot)
            {where}
            RETURN d.filename AS filename, l.lot_key AS lot_key,
                   properties(l) AS props
            ORDER BY l.lot_key SKIP $skip LIMIT $limit
            """,
            {"filename": filename, "skip": skip, "limit": _PAGE},
            max_rows=_PAGE + 1, timeout=180.0)
        rows += [dict(r) for r in page]
        if len(page) < _PAGE:
            return rows
        skip += _PAGE


def _children(keys: list[str]) -> dict[str, dict]:
    """Owned children by content, shared ones by key."""
    out: dict[str, dict] = {k: {"owned": [], "shared": []} for k in keys}
    for label, rel in OWNED:
        for i in range(0, len(keys), _PAGE):
            for r in run_read_query(
                    f"""
                    UNWIND $keys AS k
                    MATCH (l:Lot {{lot_key: k}})-[:{rel}]->(n:{label})
                    RETURN k AS lot_key, properties(n) AS props
                    """, {"keys": keys[i:i + _PAGE]},
                    max_rows=100_000, timeout=180.0):
                out[r["lot_key"]]["owned"].append(
                    {"label": label, "rel": rel, "props": r["props"]})
    for label, rel, keyprop in SHARED:
        for i in range(0, len(keys), _PAGE):
            for r in run_read_query(
                    f"""
                    UNWIND $keys AS k
                    MATCH (l:Lot {{lot_key: k}})-[:{rel}]->(n:{label})
                    RETURN k AS lot_key, n.{keyprop} AS ref
                    """, {"keys": keys[i:i + _PAGE]},
                    max_rows=100_000, timeout=180.0):
                out[r["lot_key"]]["shared"].append(
                    {"label": label, "rel": rel, "ref": r["ref"]})
    return out


def run(out_path: str, filename: str | None = None) -> int:
    lots = _lots(filename)
    if not lots:
        print("no lots to snapshot", file=sys.stderr)
        return 1
    keys = [r["lot_key"] for r in lots]
    print(f"lots: {len(keys)}", flush=True)
    kids = _children(keys)

    owned_n = sum(len(v["owned"]) for v in kids.values())
    shared_n = sum(len(v["shared"]) for v in kids.values())
    print(f"owned children captured: {owned_n}   "
          f"shared references recorded: {shared_n}", flush=True)

    payload = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "filename_filter": filename,
        "counts": {"lots": len(keys), "owned": owned_n, "shared": shared_n},
        "lots": [dict(r, **kids[r["lot_key"]]) for r in lots],
    }
    opener = gzip.open if out_path.endswith(".gz") else open
    with opener(out_path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    print(f"wrote {out_path}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True,
                    help="destination file; .gz is compressed")
    ap.add_argument("--filename", help="snapshot one Document.filename only")
    args = ap.parse_args()
    return run(args.out, args.filename)


if __name__ == "__main__":
    raise SystemExit(main())
