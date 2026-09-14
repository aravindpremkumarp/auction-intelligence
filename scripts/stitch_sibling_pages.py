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
Written on each follower: ``stitched_into``. Nothing else on a follower
changes, so ``--unstitch`` is a property removal, not a restore.

A leader that already holds an extraction gets ``extraction_stale_at`` stamped
whenever its joined text changes (or on unstitch), so
``scripts/reset_langextract_and_extract.py --stale`` re-runs it.

Usage:
    python -m scripts.stitch_sibling_pages                 # dry run (default)
    python -m scripts.stitch_sibling_pages --apply
    python -m scripts.stitch_sibling_pages --apply --only AXIS-1….jpg
    python -m scripts.stitch_sibling_pages --apply --skip 03d7249a-….jpg
    python -m scripts.stitch_sibling_pages --unstitch AXIS-1….jpg

``--only`` names a leader; it also forces a group the detector marked
ambiguous. ``--skip`` names any member and drops that whole group.
Re-runnable: a group whose joined text is unchanged is skipped.
"""
from __future__ import annotations

import argparse
import hashlib
import sys

from api.neo4j_client import run_query, run_read_query
from pipeline.notice_pages import SEPARATOR, page_groups, stitch_pages

# Every (listing, document) edge on listings carrying two or more documents
# with markdown. position is the file's index in the listing's downloads_list.
ROWS_CYPHER = """
MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
WHERE d.markdown IS NOT NULL AND d.markdown <> ''
WITH a, collect(DISTINCT d) AS docs
WHERE size(docs) >= 2
UNWIND docs AS d
WITH a, d, [i IN range(0, size(coalesce(a.downloads_list, [])) - 1)
            WHERE a.downloads_list[i] = d.filename] AS hits
RETURN a.auction_id AS listing,
       d.filename AS filename,
       d.content_sha256 AS content_sha256,
       d.markdown AS markdown,
       d.expected_lot_count AS expected_lot_count,
       d.stitched_markdown AS stitched_markdown,
       (d.extraction_json IS NOT NULL) AS has_extraction,
       CASE WHEN size(hits) = 0 THEN null ELSE hits[0] END AS position
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
WITH l
UNWIND $followers AS fn
MATCH (f:Document {filename: fn})
SET f.stitched_into = $leader
RETURN count(f) AS followers
"""

UNSTITCH_CYPHER = """
MATCH (l:Document {filename: $leader})
OPTIONAL MATCH (f:Document {stitched_into: $leader})
REMOVE f.stitched_into
SET f.extraction_stale_at = CASE WHEN f.extraction_json IS NOT NULL THEN datetime() ELSE f.extraction_stale_at END
WITH l, count(f) AS followers
REMOVE l.stitched_markdown, l.stitched_pages, l.stitched_page_offsets,
       l.stitched_at, l.stitched_expected_lot_count
SET l.extraction_stale_at = CASE WHEN l.extraction_json IS NOT NULL THEN datetime() ELSE l.extraction_stale_at END
RETURN followers
"""


def _content_key(row: dict) -> str:
    """The file's identity: its stored byte hash, else a hash of its markdown.

    Two files with the same key are twins, never pages of each other.
    """
    sha = (row.get("content_sha256") or "").strip()
    if sha:
        return sha
    return "md:" + hashlib.sha256((row.get("markdown") or "").encode("utf-8")).hexdigest()


def fetch_rows() -> list[dict]:
    rows = run_read_query(ROWS_CYPHER, None, max_rows=50_000, timeout=120.0)
    for r in rows:
        r["content_key"] = _content_key(r)
    return rows


def plan(rows: list[dict], only: set[str] | None,
         skip: set[str]) -> tuple[list[dict], list[dict]]:
    """Apply --only / --skip to the detector's output."""
    groups, ambiguous = page_groups(rows)
    if only:
        forced: list[dict] = []
        remaining_ambiguous: list[dict] = []
        for amb in ambiguous:
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
    current = docs_by_name[leader].get("stitched_markdown")
    return {"leader": leader,
            "followers": group["pages"][1:] + list(group["twins"]),
            "markdown": joined["markdown"],
            "offsets": joined["offsets"],
            "pages": list(group["pages"]),
            "expected_lot_count": joined["expected_lot_count"],
            "changed": current != joined["markdown"]}


def apply_group(built: dict) -> int:
    rows = run_query(APPLY_CYPHER, {
        "leader": built["leader"], "followers": built["followers"],
        "md": built["markdown"], "pages": built["pages"],
        "offsets": built["offsets"], "elc": built["expected_lot_count"]})
    return int(rows[0]["followers"]) if rows else 0


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
    ap.add_argument("--unstitch", help="leader filename whose stitch to remove")
    args = ap.parse_args(argv)

    if args.unstitch:
        n = unstitch(args.unstitch)
        print(f"unstitched {args.unstitch}: {n} follower(s) released")
        return 0

    rows = fetch_rows()
    groups, ambiguous = plan(rows, only=set(args.only) or None, skip=set(args.skip))
    docs_by_name = {r["filename"]: r for r in rows}
    builts = [build(g, docs_by_name) for g in groups]
    _print_plan(builts, ambiguous)
    if not args.apply:
        print("dry run — nothing written (pass --apply)")
        return 0
    written = 0
    for b in builts:
        if not b["changed"]:
            continue
        n = apply_group(b)
        written += 1
        print(f"stitched {b['leader']} ({n} follower(s))")
    print(f"wrote {written} leader(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
