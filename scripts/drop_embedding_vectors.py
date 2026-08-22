"""
scripts/drop_embedding_vectors.py
---------------------------------
Remove the orphaned embedding data left behind when retrieval moved off
vectors (docs/design/2026-08-22-retire-embeddings.md).

Nothing reads these any more — `semantic_search` is Lucene-only and
`pipeline/embeddings.py` is deleted — but the vectors are still stored, and
3072 floats per node is real disk and page-cache pressure.

    python -m scripts.drop_embedding_vectors            # dry run (default)
    python -m scripts.drop_embedding_vectors --yes      # actually delete

If Bolt (port 7687) is blocked in your environment — Claude Code on the web,
or any HTTP-only egress proxy — prefix with NEO4J_HTTP_API=1 to route through
Aura's HTTPS Query API instead.

THIS IS IRREVERSIBLE. The embed pipeline that produced these vectors no
longer exists, so there is no path to regenerate them short of reverting the
retirement commit and re-running against the Gemini API. That is why the
script dry-runs by default and needs an explicit --yes: deleting is cheap to
postpone and expensive to undo. There is no rush — the data is inert.

Property writes are batched with CALL { … } IN TRANSACTIONS so a large store
doesn't build one giant transaction.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from api.neo4j_client import run_query


#: (label, property) pairs the retired embed scripts used to write.
EMBEDDING_PROPERTIES = [
    ("AuctionProperty", "description_embedding"),  # was pipeline/embed_descriptions.py
    ("Document", "markdown_embedding"),            # was pipeline/embed_markdowns.py
    ("Document", "image_embedding"),               # was pipeline/embed_notices.py
    ("Lot", "description_embedding"),              # index existed; never populated
]

#: Vector indexes over those properties. `lot_description_embedding` was
#: created by init_graph_schema.py; the other three by the embed scripts.
VECTOR_INDEXES = [
    "property_desc_idx",
    "notice_markdown_idx",
    "notice_image_idx",
    "lot_description_embedding",
]

_BATCH = 1_000


def _count(label: str, prop: str) -> int:
    rows = run_query(
        f"MATCH (n:{label}) WHERE n.{prop} IS NOT NULL RETURN count(n) AS c"
    )
    return rows[0]["c"] if rows else 0


def run(*, apply: bool) -> int:
    mode = "APPLY" if apply else "dry-run"
    print(f"Dropping retired embedding data ({mode})\n")

    total = 0
    for label, prop in EMBEDDING_PROPERTIES:
        try:
            n = _count(label, prop)
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"  ✗ count {label}.{prop}: {exc}")
            continue
        total += n
        if n == 0:
            print(f"  · {label}.{prop}: nothing stored")
            continue
        if not apply:
            print(f"  [dry-run] would clear {label}.{prop} on {n:,} nodes")
            continue
        try:
            run_query(
                f"MATCH (n:{label}) WHERE n.{prop} IS NOT NULL "
                f"CALL {{ WITH n REMOVE n.{prop} }} "
                f"IN TRANSACTIONS OF {_BATCH} ROWS"
            )
            print(f"  ✓ cleared {label}.{prop} on {n:,} nodes")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {label}.{prop}: {exc}")

    print()
    for index in VECTOR_INDEXES:
        if not apply:
            print(f"  [dry-run] would DROP INDEX {index}")
            continue
        try:
            run_query(f"DROP INDEX {index} IF EXISTS")
            print(f"  ✓ dropped index {index}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ drop {index}: {exc}")

    if not apply:
        print(f"\n{total:,} embedded nodes found. Nothing was changed — "
              "re-run with --yes to delete. This cannot be undone.")
    else:
        print("\nDone. Retrieval is unaffected: semantic_search reads "
              "lot_description_ft and property_text_idx, both untouched.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true",
                    help="actually delete (default is a dry run)")
    args = ap.parse_args()
    return run(apply=args.yes)


if __name__ == "__main__":
    raise SystemExit(main())
