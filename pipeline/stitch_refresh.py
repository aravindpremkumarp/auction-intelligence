"""Keep a joined notice's text in step with its pages.

``scripts/stitch_sibling_pages`` writes the joined text of a two-file notice
onto page 1 (the leader) as ``stitched_markdown``, and every extraction reader
prefers it over ``markdown``. That copy is taken once. When a page's own text
is later rewritten — a re-OCR, a reviewer's block edit, a region re-ingest —
or its reviewer lot count changes, the copy goes stale and extraction keeps
reading the old text. On 2026-09-24 two TATA notices were read with page 1
missing for nine days because their page 1 was re-OCR'd after the join.

``refresh_stitches`` rebuilds the joined text of every existing group that
contains the given pages, from the pages' current text and counts, in the
page order already stored on the leader. It never creates, reorders or breaks
up a group — that stays the stitch script's job — so it is safe to call from
any writer. A leader whose text or summed count changed is rewritten and, if
it holds an extraction, stamped ``extraction_stale_at`` so
``reset_langextract_and_extract --stale`` re-reads it.

Called with no pages it checks every group: the sweep that catches a writer
which does not call it.
"""
from __future__ import annotations

from api.neo4j_client import run_query, run_read_query
from pipeline.notice_pages import stitch_pages
from pipeline.obs import get_logger

log = get_logger(__name__)

# Leaders whose group holds any of the named pages (all leaders when both
# lists are empty), each with its pages' current text in the stored order.
# A page that no longer exists drops out of ``pages``; the caller skips that
# group rather than join a notice with a page missing.
_GROUPS = """
MATCH (l:Document)
WHERE l.stitched_pages IS NOT NULL AND l.stitched_into IS NULL
  AND ($all OR any(fn IN l.stitched_pages WHERE fn IN $filenames))
UNWIND range(0, size(l.stitched_pages) - 1) AS i
OPTIONAL MATCH (p:Document {filename: l.stitched_pages[i]})
WITH l, i, p ORDER BY i
WITH l, collect(CASE WHEN p IS NULL THEN NULL ELSE
                {filename: p.filename, markdown: p.markdown,
                 expected_lot_count: p.expected_lot_count} END) AS pages
RETURN l.filename AS leader,
       l.stitched_pages AS order,
       l.stitched_markdown AS held_markdown,
       l.stitched_expected_lot_count AS held_count,
       pages
"""

# Most batch writers address a page by file_path.
_NAMES = """
UNWIND $file_paths AS fp
MATCH (d:Document {file_path: fp})
RETURN DISTINCT d.filename AS filename
"""

_WRITE = """
MATCH (l:Document {filename: $leader})
WHERE l.stitched_markdown = $held_markdown
SET l.stitched_markdown = $md,
    l.stitched_page_offsets = $offsets,
    l.stitched_expected_lot_count = $elc,
    l.stitched_at = datetime(),
    l.extraction_stale_at = CASE WHEN l.extraction_json IS NOT NULL
                                 THEN datetime() ELSE l.extraction_stale_at END
WITH l
UNWIND $followers AS fn
OPTIONAL MATCH (f:Document {filename: fn, stitched_into: $leader})
// a follower is never re-extracted, so a stale stamp on it could never clear
FOREACH (x IN CASE WHEN f IS NULL THEN [] ELSE [f] END |
  REMOVE x.extraction_stale_at)
RETURN count(DISTINCT l) AS n
"""


def plan_refresh(groups: list[dict]) -> list[dict]:
    """The groups whose joined text or summed count no longer matches their
    pages, each with the rebuilt text. Pure — no database access."""
    out = []
    for g in groups:
        pages = [p for p in (g.get("pages") or []) if p]
        if len(pages) != len(g.get("order") or []) or len(pages) < 2:
            log.warning("stitch refresh: %s has a missing page; left as is",
                        g.get("leader"))
            continue
        joined = stitch_pages(pages)
        if (joined["markdown"] == g.get("held_markdown")
                and joined["expected_lot_count"] == g.get("held_count")):
            continue
        out.append({"leader": g["leader"],
                    "held_markdown": g.get("held_markdown"),
                    "followers": [p["filename"] for p in pages[1:]],
                    **joined})
    return out


def refresh_stitches(filenames=(), file_paths=()) -> list[str]:
    """Rebuild the joined text of every group holding one of these pages.

    With neither argument, every group is checked. Returns the leaders that
    were rewritten. Never raises: a writer's own save must not fail because
    this follow-up did — it logs instead, and the sweep catches it later.
    """
    filenames = [f for f in filenames if f]
    file_paths = [f for f in file_paths if f]
    check_all = not filenames and not file_paths
    try:
        if file_paths:
            filenames += [r["filename"] for r in run_read_query(
                _NAMES, {"file_paths": file_paths}, timeout=60.0,
                max_rows=50_000)]
        groups = run_read_query(_GROUPS, {"all": check_all,
                                          "filenames": filenames},
                                timeout=60.0, max_rows=10_000)
        done = []
        for r in plan_refresh(groups):
            rows = run_query(_WRITE, {
                "leader": r["leader"], "held_markdown": r["held_markdown"],
                "md": r["markdown"], "offsets": r["offsets"],
                "elc": r["expected_lot_count"], "followers": r["followers"]})
            if rows and rows[0].get("n"):
                done.append(r["leader"])
        if done:
            log.info("stitch refresh: rebuilt joined text of %s", done)
        return done
    except Exception:  # noqa: BLE001 — see docstring
        log.warning("stitch refresh failed for %s %s", filenames, file_paths,
                    exc_info=True)
        return []
