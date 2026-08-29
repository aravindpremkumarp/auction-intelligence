"""
scripts/collapse_identical_documents.py
---------------------------------------
Collapse a notice stored twice **on one auction** into a single Document.

The safety rule, and why it is the whole point
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``scripts/find_duplicate_notices.py`` finds byte-identical notices. Most of them
must NOT be collapsed. A bank publishing one notice for six lots has it attached
to six auctions, and each of those Documents carries its own extraction — its own
:Lot, :Contact, :Bank, :EMDAccount edges for *that* auction's property. In the
corpus, 17 of 23 identical-byte groups are that shape: the ``KARNTK1781938…``
sextet is one notice covering a flat in Sholinganallur, land in Ambattur,
Sriperumbudur and Poonamallee, a house in Avadi and a flat in Tondiarpet — six
real, different lots. Deleting five of those six deletes five auctions' data.

So this script collapses a group only when **every copy hangs off the same single
AuctionProperty**. That is the one case where the second node is pure redundancy:
the page renders the same notice twice and counts its lots twice. Everything else
is refused by construction, not by judgement — 15 auctions carry two documents and
only 3 of those pairs are byte-identical; the other 12 are a notice and its
corrigendum, and are left alone.

What "delete" has to mean here
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
A ``DETACH DELETE`` of the Document alone is not enough and not safe:

* Its :Lot is reachable only through it (``HAS_LOT`` is the Lot's sole inbound
  edge), so deleting the Document strands the Lot — the duplicate lot survives
  with no notice attached.
* But the Lot's own children are a mix: :Measurement, :Boundary, :Fact and
  :Parcel belong to that lot, while :Borrower, :District, :Taluk and :Auction are
  shared vocabulary that other lots point at too.

The rule that separates them without a hand-maintained label list is
**disconnection**: after the parent goes, delete a former child only if it has no
relationships left at all. A shared node always keeps one, so it is never
touched; a lot-specific value node keeps none, so it never becomes debris. The
sweep is confined to nodes that were reachable from the doomed Document, so it
can never reach an unrelated isolated node elsewhere in the graph.

Reversibility
~~~~~~~~~~~~~
Every deleted node's full properties and edges are written to a JSON backup
before anything is removed. ``--dry-run`` is the default; ``--apply`` executes.

Runs over HTTPS (Neo4j Query API v2 + R2 source), like
``scripts/find_duplicate_notices.py``, whose selection and hashing this reuses.

Usage:
    python -m scripts.collapse_identical_documents --dry-run
    python -m scripts.collapse_identical_documents --apply
    python -m scripts.collapse_identical_documents --apply --auction 821956

Auth: NEO4J_URI/USERNAME/PASSWORD(/DATABASE) in the environment.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import requests

from pipeline.ink_fingerprint import content_hash
from scripts.find_duplicate_notices import nq


# Documents on an auction that carries more than one. Everything the survivor
# ranking needs comes back with them, so the choice is made on the node's real
# richness rather than on its name or its upload order alone.
CANDIDATES_CYPHER = """
MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
WHERE $auction IS NULL OR a.auction_id = $auction
WITH a, collect(DISTINCT d) AS docs
WHERE size(docs) > 1
UNWIND docs AS d
OPTIONAL MATCH (d)-[rel]-()
OPTIONAL MATCH (d)-[:HAS_LOT]->(l:Lot)
RETURN a.auction_id            AS auction_id,
       elementId(d)            AS id,
       d.filename              AS filename,
       d.public_url            AS public_url,
       toString(d.uploaded_at) AS uploaded_at,
       count(DISTINCT rel)     AS rels,
       size(keys(d))           AS props,
       count(DISTINCT l)       AS lots
ORDER BY auction_id, filename
"""


def _rank(doc: dict) -> tuple:
    """Sort key for the survivor — richest node wins.

    Relationship count first: the node that extracted more of the notice is the
    one worth keeping, and in this corpus that is what actually separates two
    otherwise identical copies (one had an EMD account and a contact the other
    lacked). Property count breaks the tie, then the *earlier* upload — the
    original rather than the accidental re-post — then elementId so a run is
    reproducible.
    """
    return (doc["rels"], doc["props"], _neg_time(doc["uploaded_at"]), doc["id"])


def _neg_time(iso: str | None) -> tuple:
    """Order timestamps so earlier sorts higher under ``reverse=True``.

    Missing timestamps sort last rather than first: a node that never recorded
    an upload is not evidence of being the original.
    """
    return (0,) if not iso else (1, [-ord(c) for c in iso])


def fingerprint(docs: list[dict]) -> None:
    """Attach each document's content hash, in place. Failures stay ``None``."""
    for d in docs:
        try:
            r = requests.get(d["public_url"], timeout=120)
            r.raise_for_status()
            d["sha"] = content_hash(r.content)
        except Exception as e:
            d["sha"] = None
            d["error"] = f"{type(e).__name__}: {e}"


def collapsible(docs: list[dict]) -> list[tuple[str, str, list[dict]]]:
    """Groups of identical documents that sit on one auction.

    Returns ``[(auction_id, sha, [documents])]``. A group needs two or more
    copies of the *same bytes* on the *same* auction; a pair of different files
    on one auction (a notice and its corrigendum) is not a duplicate and never
    appears here.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for d in docs:
        if d.get("sha"):
            groups.setdefault((d["auction_id"], d["sha"]), []).append(d)
    return [(a, s, g) for (a, s), g in sorted(groups.items()) if len(g) > 1]


BACKUP_CYPHER = """
MATCH (d:Document) WHERE elementId(d) = $id
OPTIONAL MATCH (d)-[:HAS_LOT]->(l:Lot)
WITH d, collect(DISTINCT l) AS lots
RETURN properties(d)                                        AS document,
       [x IN lots | properties(x)]                          AS lots,
       [(d)-[r]-(o) | {type: type(r), other: elementId(o),
                       labels: labels(o)}]                  AS doc_edges,
       [x IN lots | [(x)-[r]-(o) | {lot: elementId(x), type: type(r),
                                    other: elementId(o),
                                    labels: labels(o)}]]     AS lot_edges,
       [x IN lots | elementId(x)]                           AS lot_ids,
       [x IN lots | [(x)--(o) | elementId(o)]]              AS lot_neighbours
"""


def delete_document(doc_id: str) -> dict:
    """Remove one Document, its now-parentless lots, and their debris.

    Three passes, in this order, because each one is what makes the next safe:

    1. The Document itself.
    2. Any lot it owned that no Document points at any more.
    3. Any former neighbour of those lots left with no relationships at all —
       the lot-specific :Measurement / :Boundary / :Fact / :Parcel nodes. A
       shared node still has an edge, so this never reaches one.

    Returns the counts actually removed.
    """
    captured = nq(BACKUP_CYPHER, {"id": doc_id})
    if not captured:
        return {"documents": 0, "lots": 0, "children": 0}
    row = captured[0]
    lot_ids = row[4] or []
    neighbours = sorted({n for group in (row[5] or []) for n in group})

    nq("MATCH (d:Document) WHERE elementId(d) = $id DETACH DELETE d",
       {"id": doc_id})
    lots = nq(
        """
        UNWIND $ids AS id
        MATCH (l:Lot) WHERE elementId(l) = id
          AND NOT (l)<-[:HAS_LOT]-(:Document)
        DETACH DELETE l
        RETURN count(*) AS n
        """, {"ids": lot_ids})
    children = nq(
        """
        UNWIND $ids AS id
        MATCH (n) WHERE elementId(n) = id AND NOT (n)--()
        DELETE n
        RETURN count(*) AS n
        """, {"ids": neighbours})
    return {"documents": 1,
            "lots": (lots[0][0] if lots else 0),
            "children": (children[0][0] if children else 0)}


def snapshot(doc_id: str) -> dict:
    """Everything about a document needed to recreate it, for the backup file."""
    captured = nq(BACKUP_CYPHER, {"id": doc_id})
    if not captured:
        return {}
    row = captured[0]
    return {"id": doc_id, "document": row[0], "lots": row[1],
            "document_edges": row[2],
            "lot_edges": [e for group in (row[3] or []) for e in group]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="perform the deletion (default is a dry run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit no-op mode; the default when --apply is absent")
    ap.add_argument("--auction", default=None,
                    help="restrict to a single auction_id")
    ap.add_argument("--backup", default=None,
                    help="where to write the pre-deletion JSON backup "
                         "(default: collapse-backup-<timestamp>.json)")
    args = ap.parse_args()

    rows = nq(CANDIDATES_CYPHER, {"auction": args.auction})
    docs = [{"auction_id": r[0], "id": r[1], "filename": r[2], "public_url": r[3],
             "uploaded_at": r[4], "rels": r[5], "props": r[6], "lots": r[7]}
            for r in rows]
    auctions = {d["auction_id"] for d in docs}
    print(f"{len(auctions)} auction(s) carry more than one Document "
          f"({len(docs)} documents)")
    if not docs:
        return 0

    fingerprint(docs)
    failed = [d for d in docs if not d.get("sha")]
    for d in failed:
        print(f"  [skip] {d['filename']}: {d.get('error')}")

    groups = collapsible(docs)
    if not groups:
        print("No auction carries the same notice twice — nothing to collapse.")
        return 0

    print(f"\n{len(groups)} auction(s) carry the SAME notice twice:\n")
    plan: list[dict] = []
    for auction_id, sha, group in groups:
        ordered = sorted(group, key=_rank, reverse=True)
        keep, drop = ordered[0], ordered[1:]
        print(f"  auction {auction_id}  ({sha[:12]}…)")
        print(f"    keep  {keep['filename'][:44]:46} "
              f"rels={keep['rels']} props={keep['props']} lots={keep['lots']}")
        for d in drop:
            print(f"    DROP  {d['filename'][:44]:46} "
                  f"rels={d['rels']} props={d['props']} lots={d['lots']}")
        plan.append({"auction_id": auction_id, "keep": keep, "drop": drop})

    doomed = [d for p in plan for d in p["drop"]]
    if not args.apply:
        print(f"\nDry run — nothing deleted. {len(doomed)} Document(s) would go, "
              f"with their lots. Re-run with --apply.")
        return 0

    path = args.backup or (
        f"collapse-backup-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json")
    with open(path, "w") as fh:
        json.dump([snapshot(d["id"]) for d in doomed], fh, indent=1, default=str)
    print(f"\nBacked up {len(doomed)} document(s) to {path}")

    totals = {"documents": 0, "lots": 0, "children": 0}
    for d in doomed:
        removed = delete_document(d["id"])
        for k, v in removed.items():
            totals[k] += v
        print(f"  deleted {d['filename'][:44]:46} "
              f"lots={removed['lots']} child-nodes={removed['children']}")

    print(f"\nRemoved {totals['documents']} document(s), {totals['lots']} lot(s), "
          f"{totals['children']} orphaned child node(s).")
    for p in plan:
        check = nq(
            """
            MATCH (a:AuctionProperty {auction_id: $id})-[:HAS_DOCUMENT]->(d:Document)
            OPTIONAL MATCH (d)-[:HAS_LOT]->(l:Lot)
            RETURN count(DISTINCT d), count(DISTINCT l)
            """, {"id": p["auction_id"]})
        docs_left, lots_left = check[0] if check else (None, None)
        print(f"  auction {p['auction_id']}: {docs_left} document(s), "
              f"{lots_left} lot(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
