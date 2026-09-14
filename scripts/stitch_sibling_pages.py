"""
scripts/stitch_sibling_pages.py
-------------------------------
Join the pages of a two-file sale notice so extraction reads them as one.

A listing showing two different notice images is one notice on two sheets.
This script finds those pairs from the listing's own file order
(``pipeline/notice_pages``), writes the joined text onto page 1 — the *leader*
— and points page 2 (the *follower*) at it. Every extraction reader then picks
``coalesce(d.stitched_markdown, d.markdown)`` and skips followers, so the model
sees the whole notice once.

Written on the leader: ``stitched_markdown``, ``stitched_pages``,
``stitched_page_offsets``, ``stitched_at``, ``stitched_expected_lot_count``
(the sum of the pages' reviewer counts, or null if any page lacks one).
Written on each follower: ``stitched_into`` (and its ``extraction_stale_at``
is removed — nothing re-extracts a follower, so the stamp could never clear).

A follower's old lots are cleared on ``--apply``. Promote and apply skip
followers, so the ``(:Lot)`` nodes page 2 was promoted into — and any
``IS_LOT`` edge to them — would otherwise sit beside the leader's new lots
forever. The deletion is promote's own per-document rebuild
(``pipeline.promote_extractions.rebuild_document_lots``): the lot and the
children it owns outright go; shared nodes (Parcel, Identifier, Borrower,
places) are only detached. Pass ``--keep-follower-lots`` to skip it. Only
followers of groups written in this run are cleared.

A leader that already holds an extraction gets ``extraction_stale_at`` stamped
whenever its joined text or summed lot count changes (or on unstitch), so
``scripts/reset_langextract_and_extract.py --stale`` re-runs it.

Usage:
    python -m scripts.stitch_sibling_pages                 # dry run (default)
    python -m scripts.stitch_sibling_pages --apply
    python -m scripts.stitch_sibling_pages --apply --keep-follower-lots
    python -m scripts.stitch_sibling_pages --apply --only AXIS-1….jpg
    python -m scripts.stitch_sibling_pages --apply --skip 03d7249a-….jpg
    python -m scripts.stitch_sibling_pages --unstitch AXIS-1….jpg

``--only`` names a leader; it also forces a group the detector marked
ambiguous (never a "twin outside group" report). ``--skip`` names any member
and drops that whole group. Re-runnable: a group whose joined text and lot
count are unchanged is skipped.
"""
from __future__ import annotations

import argparse
import sys

from api.neo4j_client import run_query, run_read_query
from pipeline.notice_pages import (
    SEPARATOR, TWIN_OUTSIDE_GROUP, page_groups, stitch_pages,
)
from pipeline.notice_twins import text_key
from pipeline.promote_extractions import rebuild_document_lots

# (a) Candidate documents: every document with markdown that sits on at least
# one listing carrying two or more such documents. Each document once — the
# markdown is the heavy column, so it is never repeated per edge.
CANDIDATES_CYPHER = """
MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
WHERE d.markdown IS NOT NULL AND d.markdown <> ''
WITH a, collect(DISTINCT d) AS docs
WHERE size(docs) >= 2
UNWIND docs AS d
WITH DISTINCT d
RETURN d.filename AS filename,
       d.markdown AS markdown,
       d.expected_lot_count AS expected_lot_count,
       d.stitched_markdown AS stitched_markdown,
       d.stitched_expected_lot_count AS stitched_expected_lot_count,
       (d.extraction_json IS NOT NULL) AS has_extraction
ORDER BY filename
"""

# (b) Every HAS_DOCUMENT edge of those candidates — including a listing where
# the candidate is the only document, which is exactly what the same-listing
# test needs to see. position is the file's index in downloads_list.
EDGES_CYPHER = """
UNWIND $filenames AS fn
MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document {filename: fn})
WITH DISTINCT a, d
WITH a, d, [i IN range(0, size(coalesce(a.downloads_list, [])) - 1)
            WHERE a.downloads_list[i] = d.filename] AS hits
RETURN a.auction_id AS listing,
       d.filename AS filename,
       CASE WHEN size(hits) = 0 THEN null ELSE hits[0] END AS position
"""

# How many lots each follower holds — what --apply would clear.
FOLLOWER_LOTS_CYPHER = """
UNWIND $filenames AS fn
OPTIONAL MATCH (:Document {filename: fn})-[:HAS_LOT]->(l:Lot)
RETURN fn AS filename, count(l) AS lots
"""

APPLY_CYPHER = """
MATCH (l:Document {filename: $leader})
SET l.stitched_markdown = $md,
    l.stitched_pages = $pages,
    l.stitched_page_offsets = $offsets,
    l.stitched_at = datetime(),
    l.stitched_expected_lot_count = $elc,
    // The stored entities were read off a different text. Keep them (stale
    // beats absent) and queue a re-run — same contract as verify_classification.
    l.extraction_stale_at = CASE WHEN l.extraction_json IS NOT NULL THEN datetime() ELSE l.extraction_stale_at END
// a leader is never itself a follower (it may have been one in an older plan)
REMOVE l.stitched_into
WITH l
UNWIND $followers AS fn
MATCH (f:Document {filename: fn})
SET f.stitched_into = $leader
// nothing re-extracts a follower, so its stamp could never be cleared
REMOVE f.extraction_stale_at
RETURN count(f) AS followers
"""

UNSTITCH_CYPHER = """
MATCH (l:Document {filename: $leader})
OPTIONAL MATCH (f:Document {stitched_into: $leader})
FOREACH (x IN CASE WHEN f IS NULL THEN [] ELSE [f] END |
  REMOVE x.stitched_into
  SET x.extraction_stale_at = CASE WHEN x.extraction_json IS NOT NULL THEN datetime() ELSE x.extraction_stale_at END)
WITH l, count(f) AS followers
REMOVE l.stitched_markdown, l.stitched_pages, l.stitched_page_offsets,
       l.stitched_at, l.stitched_expected_lot_count
SET l.extraction_stale_at = CASE WHEN l.extraction_json IS NOT NULL THEN datetime() ELSE l.extraction_stale_at END
RETURN followers
"""


def _content_key(row: dict) -> str:
    """The file's identity: the SHA-256 of its markdown, always.

    One key space only. Mixing in ``content_sha256`` gave a page and its byte
    twin different keys when only one had the hash, and a page was stitched to
    itself. Two files with the same key are twins, never pages of each other.
    """
    return text_key(row) or ""


def fetch_rows() -> tuple[list[dict], dict[str, dict]]:
    """Read the detector's input.

    Returns ``(rows, docs_by_name)``: ``rows`` are ``page_groups`` rows
    (``listing``, ``filename``, ``content_key``, ``position``), one per edge;
    ``docs_by_name`` holds each candidate document once, with its markdown.
    """
    docs = run_read_query(CANDIDATES_CYPHER, None, max_rows=50_000, timeout=120.0)
    docs_by_name: dict[str, dict] = {}
    for d in docs:
        d["content_key"] = _content_key(d)
        docs_by_name.setdefault(d["filename"], d)
    if not docs_by_name:
        return [], {}
    edges = run_read_query(EDGES_CYPHER, {"filenames": list(docs_by_name)},
                           max_rows=50_000, timeout=120.0)
    rows = [{"listing": e["listing"], "filename": e["filename"],
             "content_key": docs_by_name[e["filename"]]["content_key"],
             "position": e["position"]}
            for e in edges if e["filename"] in docs_by_name]
    return rows, docs_by_name


def plan(rows: list[dict], only: set[str] | None,
         skip: set[str]) -> tuple[list[dict], list[dict]]:
    """Apply --only / --skip to the detector's output."""
    groups, ambiguous = page_groups(rows)
    if only:
        forced: list[dict] = []
        remaining_ambiguous: list[dict] = []
        for amb in ambiguous:
            # a twin left out of its group holds the same text as the leader;
            # forcing it would stitch a page onto itself
            if amb["reason"].startswith(TWIN_OUTSIDE_GROUP):
                remaining_ambiguous.append(amb)
                continue
            # take the members in their position order on the first listing
            # that carries them all
            pos = {}
            for r in rows:
                if r["filename"] in amb["filenames"] and r["position"] is not None:
                    pos.setdefault(r["filename"], r["position"])
            ordered = sorted(amb["filenames"], key=lambda f: (pos.get(f, 1 << 30), f))
            # Force the group only if the actual leader (ordered[0]) is in --only
            if ordered[0] in only:
                forced.append({"pages": ordered, "twins": []})
            else:
                remaining_ambiguous.append(amb)
        groups = [g for g in groups + forced if g["pages"][0] in only]
        ambiguous = remaining_ambiguous
    if skip:
        groups = [g for g in groups
                  if not (set(g["pages"]) | set(g["twins"])) & skip]
    return groups, ambiguous


def build(group: dict, docs_by_name: dict[str, dict]) -> dict:
    pages = [{"filename": fn, "markdown": docs_by_name[fn]["markdown"],
              "expected_lot_count": docs_by_name[fn].get("expected_lot_count")}
             for fn in group["pages"]]
    joined = stitch_pages(pages)
    leader = group["pages"][0]
    held = docs_by_name[leader]
    changed = (held.get("stitched_markdown") != joined["markdown"]
               or held.get("stitched_expected_lot_count") != joined["expected_lot_count"])
    return {"leader": leader,
            "followers": group["pages"][1:] + list(group["twins"]),
            "markdown": joined["markdown"],
            "offsets": joined["offsets"],
            "pages": list(group["pages"]),
            "expected_lot_count": joined["expected_lot_count"],
            "changed": changed}


def apply_group(built: dict) -> int:
    rows = run_query(APPLY_CYPHER, {
        "leader": built["leader"], "followers": built["followers"],
        "md": built["markdown"], "pages": built["pages"],
        "offsets": built["offsets"], "elc": built["expected_lot_count"]})
    return int(rows[0]["followers"]) if rows else 0


def count_follower_lots(filenames: list[str]) -> dict[str, int]:
    if not filenames:
        return {}
    rows = run_read_query(FOLLOWER_LOTS_CYPHER, {"filenames": filenames},
                          max_rows=50_000, timeout=120.0)
    return {r["filename"]: int(r["lots"] or 0) for r in rows}


def clear_follower_lots(filenames: list[str]) -> dict[str, int]:
    """Delete each follower's lots via promote's per-document rebuild.

    Returns lots deleted per follower.
    """
    return {fn: rebuild_document_lots(fn) for fn in filenames}


def unstitch(leader: str) -> int:
    rows = run_query(UNSTITCH_CYPHER, {"leader": leader})
    return int(rows[0]["followers"]) if rows else 0


def _print_plan(builts: list[dict], ambiguous: list[dict]) -> None:
    for b in builts:
        state = "unchanged" if not b["changed"] else "write"
        elc = b["expected_lot_count"] if b["expected_lot_count"] is not None else "?"
        print(f"[{state}] {b['leader']} <- {', '.join(b['followers'])}  "
              f"({len(b['markdown'])} chars, lots={elc})")
    for amb in ambiguous:
        print(f"[ambiguous] {' | '.join(amb['filenames'])}: {amb['reason']}")
    print(f"{len(builts)} group(s), {len(ambiguous)} ambiguous")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan and write nothing (the default)")
    ap.add_argument("--apply", action="store_true", help="write the stitches")
    ap.add_argument("--only", action="append", default=[],
                    help="leader filename to stitch (repeatable); forces an ambiguous group")
    ap.add_argument("--skip", action="append", default=[],
                    help="any member filename whose group must not be stitched (repeatable)")
    ap.add_argument("--keep-follower-lots", action="store_true",
                    help="with --apply, leave each follower's old lots in place")
    ap.add_argument("--unstitch", help="leader filename whose stitch to remove")
    args = ap.parse_args(argv)

    if args.unstitch:
        n = unstitch(args.unstitch)
        print(f"unstitched {args.unstitch}: {n} follower(s) released")
        return 0

    rows, docs_by_name = fetch_rows()
    groups, ambiguous = plan(rows, only=set(args.only) or None, skip=set(args.skip))
    builts = [build(g, docs_by_name) for g in groups]
    _print_plan(builts, ambiguous)
    to_write = [b for b in builts if b["changed"]]
    if not args.keep_follower_lots:
        counts = count_follower_lots([fn for b in to_write for fn in b["followers"]])
        for b in to_write:
            for fn in b["followers"]:
                print(f"[lots to clear] {fn}: {counts.get(fn, 0)}")
    if not args.apply:
        print("dry run — nothing written (pass --apply)")
        return 0
    written = 0
    for b in to_write:
        n = apply_group(b)
        written += 1
        print(f"stitched {b['leader']} ({n} follower(s))")
        if not args.keep_follower_lots:
            cleared = clear_follower_lots(b["followers"])
            print(f"  cleared {sum(cleared.values())} lot(s) from "
                  f"{len(cleared)} follower(s)")
    print(f"wrote {written} leader(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
