"""Audit how listings are linked to the lots read out of their sale notice.

READ-ONLY. This is Phase 0 of replacing `AuctionProperty.resolved_lot_key`
with a real `(:AuctionProperty)-[:IS_LOT]->(:Lot)` relationship; it writes
nothing and exists to be read before anything else in that migration runs.

Why the string has to go
------------------------
`resolved_lot_key` is `"<filename>#<lot_index>"`, and `lot_index` is the
extraction model's own numbering. Re-extract a notice and the lots renumber,
so a key saying `#3` still RESOLVES — to a different property. Nothing raises,
nothing logs. A stale pointer that silently hits the wrong row is worse than
one that dangles, and only a real relationship makes it impossible.

`promote_extractions` compounds it with `MERGE (l:Lot {lot_key: ...})`: a
re-extraction reuses the old lot node, so the new lot 3 inherits the previous
run's boundaries and identifiers.

What this reports
-----------------
  A  listings whose resolved_lot_key matches no Lot          (broken pointer)
  B  documents whose Lot nodes disagree with their current
     extraction's lot count                                  (stale lots)
  C  Lot nodes with no Document                              (orphans)
  D  lot_index values that are not plain integers            (key is free text)
  E  child nodes, split into lot-owned vs shared             (rebuild safety)
  F  human lot decisions and whether they survive a rebuild
  G  one lot claimed by more than one listing                (rivalry)
  H  listings on multi-lot notices with no lot at all        (unresolved)

E is the one that governs Phase 3. A lot owns its Measurement / Boundary /
Schedule / Auction outright (each keyed by lot_key, one lot each), so those
die with it. Identifier / Borrower / Parcel are SHARED across lots — one
identifier is referenced by 154 of them — so a rebuild must detach those and
never delete them.

Usage:
    NEO4J_HTTP_API=1 python -m scripts.audit_lot_links
    NEO4J_HTTP_API=1 python -m scripts.audit_lot_links --json out.json
    NEO4J_HTTP_API=1 python -m scripts.audit_lot_links --limit 20

Auth: NEO4J_URI/USERNAME/PASSWORD(/DATABASE).
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from api.neo4j_client import run_read_query

#: Children a lot owns outright — each is keyed by lot_key and belongs to
#: exactly one lot, so a rebuild deletes them with it.
OWNED = (("Measurement", "HAS_EXTENT"), ("Boundary", "HAS_BOUNDARY"),
         ("Schedule", "HAS_SCHEDULE"), ("Auction", "OFFERED_IN"))
#: Children shared BETWEEN lots. A rebuild must detach, never delete: removing
#: one damages notices this run never touched.
SHARED = (("Identifier", "MENTIONS_IDENTIFIER"), ("Borrower", "HAS_PARTY"),
          ("Parcel", "IS_PARCEL"))

_INT_INDEX = re.compile(r"^[0-9]+$")


def q(cypher: str, params: dict | None = None, max_rows: int = 20_000):
    return run_read_query(cypher, params, max_rows=max_rows, timeout=120.0)


def _lots_in_extraction(extraction_json: str | None) -> set[str]:
    """Distinct lot_index values the CURRENT extraction produces.

    str() because the model returns lot_index as a JSON number about as often
    as a string — pipeline.apply_extractions.group_lots normalises the same way.
    """
    try:
        ents = json.loads(extraction_json or "[]") or []
    except (TypeError, ValueError):
        return set()
    return {str((e.get("attrs") or {}).get("lot_index") or "1") for e in ents}


def section(title: str) -> None:
    print(f"\n{'─' * 72}\n{title}\n{'─' * 72}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", metavar="PATH", help="also write the findings as JSON")
    ap.add_argument("--limit", type=int, default=10,
                    help="rows to print per finding (default 10)")
    args = ap.parse_args(argv)
    out: dict = {}
    N = args.limit

    # ── scale ────────────────────────────────────────────────────────────────
    section("SCALE")
    counts = q("""
        MATCH (d:Document) WITH count(d) AS docs
        MATCH (l:Lot) WITH docs, count(l) AS lots
        MATCH (a:AuctionProperty)
        RETURN docs, lots, count(a) AS listings,
               sum(CASE WHEN a.resolved_lot_key IS NOT NULL THEN 1 ELSE 0 END) AS keyed
    """)[0]
    out["scale"] = dict(counts)
    for k in ("docs", "lots", "listings", "keyed"):
        print(f"  {k:26} {counts[k]}")

    # ── A: broken pointers ───────────────────────────────────────────────────
    section("A  listings whose resolved_lot_key matches no Lot  (broken pointer)")
    rows = q("""
        MATCH (a:AuctionProperty) WHERE a.resolved_lot_key IS NOT NULL
        OPTIONAL MATCH (l:Lot {lot_key: a.resolved_lot_key})
        WITH a WHERE l IS NULL
        RETURN a.auction_id AS aid, a.resolved_lot_key AS key
        ORDER BY key
    """)
    out["broken_pointers"] = [dict(r) for r in rows]
    print(f"  {len(rows)} listing(s)")
    for r in rows[:N]:
        print(f"    {r['aid']:>10}  {r['key']}")

    # ── B: stale lots ────────────────────────────────────────────────────────
    section("B  documents whose Lot nodes disagree with the current extraction")
    rows = q("""
        MATCH (d:Document) WHERE d.extraction_json IS NOT NULL
        OPTIONAL MATCH (d)-[:HAS_LOT]->(l:Lot)
        WITH d, count(l) AS n_lots, collect(l.lot_index) AS idx
        WHERE n_lots > 0
        RETURN d.filename AS fn, n_lots, idx, d.extraction_json AS ej
    """)
    stale, missing_idx = [], []
    for r in rows:
        new = _lots_in_extraction(r["ej"])
        if not new:
            continue
        if len(new) != r["n_lots"]:
            stale.append({"filename": r["fn"], "lot_nodes": r["n_lots"],
                          "extraction_lots": len(new)})
        gone = sorted(set(map(str, r["idx"] or [])) - new)
        if gone:
            missing_idx.append({"filename": r["fn"], "orphan_lot_index": gone})
    out["stale_lot_counts"] = stale
    out["lot_index_no_longer_produced"] = missing_idx
    print(f"  {len(stale)} document(s) where the Lot count != the extraction's lot count")
    for s in sorted(stale, key=lambda s: -abs(s['lot_nodes'] - s['extraction_lots']))[:N]:
        print(f"    {s['filename'][:52]:52} lots={s['lot_nodes']:>3} "
              f"extraction={s['extraction_lots']:>3}")
    print(f"\n  {len(missing_idx)} document(s) hold a Lot whose index the extraction "
          f"no longer produces")
    for m in missing_idx[:N]:
        print(f"    {m['filename'][:52]:52} {m['orphan_lot_index'][:5]}")

    # ── C: orphan lots ───────────────────────────────────────────────────────
    section("C  Lot nodes with no Document")
    rows = q("""
        MATCH (l:Lot) WHERE NOT (:Document)-[:HAS_LOT]->(l)
        RETURN l.lot_key AS key ORDER BY key
    """)
    out["orphan_lots"] = [r["key"] for r in rows]
    print(f"  {len(rows)} orphan Lot(s)")
    for r in rows[:N]:
        print(f"    {r['key']}")

    # ── D: the key is free text ──────────────────────────────────────────────
    section("D  lot_index values that are not plain integers")
    rows = q("MATCH (l:Lot) RETURN l.lot_index AS idx, count(*) AS n")
    odd = sorted(((r["idx"], r["n"]) for r in rows
                  if not _INT_INDEX.match(str(r["idx"] or ""))),
                 key=lambda t: -t[1])
    out["non_integer_lot_index"] = [{"lot_index": i, "lots": n} for i, n in odd]
    print(f"  {len(odd)} distinct non-integer value(s) — the primary key embeds "
          f"raw model output")
    for i, n in odd[:N]:
        print(f"    {str(i)!r:24} on {n} lot(s)")

    # ── E: rebuild blast radius ──────────────────────────────────────────────
    section("E  children of a Lot — what a rebuild may delete vs must only detach")
    print(f"  {'node':14} {'via':22} {'nodes':>7} {'max lots sharing':>17}  verdict")
    child = {}
    for label, rel in OWNED + SHARED:
        r = q(f"""
            MATCH (l:Lot)-[:{rel}]->(n:{label})
            WITH n, count(DISTINCT l) AS lots
            RETURN count(n) AS nodes, max(lots) AS max_lots,
                   sum(CASE WHEN lots > 1 THEN 1 ELSE 0 END) AS shared
        """)[0]
        owned = (label, rel) in OWNED
        verdict = "DELETE with lot" if r["max_lots"] == 1 else "DETACH ONLY"
        # An "owned" node found on more than one lot would break Phase 3's
        # safety argument, so say so loudly rather than let it pass.
        alarm = "  <-- EXPECTED OWNED, IS SHARED" if owned and r["max_lots"] != 1 else ""
        child[label] = {"via": rel, "nodes": r["nodes"], "max_lots": r["max_lots"],
                        "shared_by_more_than_one": r["shared"],
                        "verdict": verdict, "unexpected": bool(alarm)}
        print(f"  {label:14} {rel:22} {r['nodes']:>7} {r['max_lots']:>17}  "
              f"{verdict}{alarm}")
    out["children"] = child
    deletable = sum(v["nodes"] for k, v in child.items()
                    if k in {lbl for lbl, _ in OWNED})
    print(f"\n  a full rebuild would delete ~{deletable} owned child node(s) "
          f"plus every Lot, and detach (never delete) the shared ones")

    # ── F: human decisions ───────────────────────────────────────────────────
    section("F  human lot decisions — do they survive a lot rebuild?")
    rows = q("""
        MATCH (r:ResolutionDecision {kind: 'lot-match'})
        WHERE r.decided_by IS NOT NULL AND NOT r.decided_by STARTS WITH 'system:'
        RETURN r.decided_by AS who, count(*) AS n
    """)
    out["human_decisions"] = [dict(r) for r in rows]
    total = sum(r["n"] for r in rows)
    print(f"  {total} decision(s) made by a person")
    for r in rows:
        print(f"    {r['who']}: {r['n']}")
    print("  These are keyed by auction_id, not by lot, so a rebuild does not")
    print("  destroy them — but they must be RE-ANCHORED to the new lot.")

    # ── G: rivalry ───────────────────────────────────────────────────────────
    section("G  one lot claimed by more than one listing")
    rows = q("""
        MATCH (a:AuctionProperty) WHERE a.resolved_lot_key IS NOT NULL
        WITH a.resolved_lot_key AS key, collect(a.auction_id) AS aids
        WHERE size(aids) > 1
        RETURN key, aids ORDER BY size(aids) DESC
    """)
    out["contested_lots"] = [dict(r) for r in rows]
    print(f"  {len(rows)} lot(s) claimed by two or more listings")
    for r in rows[:N]:
        print(f"    {r['key'][:52]:52} {r['aids']}")

    # ── H: unresolved ────────────────────────────────────────────────────────
    section("H  listings on a multi-lot notice with no lot at all")
    r = q("""
        MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
        WHERE d.expected_lot_count > 1
        WITH DISTINCT a
        RETURN count(a) AS total,
               sum(CASE WHEN a.resolved_lot_key IS NULL THEN 1 ELSE 0 END) AS unresolved
    """)[0]
    out["multi_lot_listings"] = dict(r)
    pct = (r["unresolved"] / r["total"] * 100) if r["total"] else 0
    print(f"  {r['unresolved']} of {r['total']} unresolved ({pct:.1f}%)")

    # ── verdict ──────────────────────────────────────────────────────────────
    section("PHASE 0 VERDICT")
    blockers = []
    if any(v["unexpected"] for v in child.values()):
        blockers.append("a node expected to be lot-owned is shared between lots — "
                        "Phase 3's delete set is not safe as written")
    if out["orphan_lots"]:
        blockers.append(f"{len(out['orphan_lots'])} orphan Lot(s) have no document to "
                        "rebuild them from — they would be deleted, not replaced")
    if blockers:
        print("  BLOCKED:")
        for b in blockers:
            print(f"    - {b}")
    else:
        print("  No blocker found. Phases 1-2 (add the edge, switch readers) are safe.")
        print("  Phase 3 still needs a snapshot and a staged rollout.")
    out["blockers"] = blockers

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, ensure_ascii=False, default=str)
        print(f"\n  findings written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
