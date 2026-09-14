# Stitch Sibling Pages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A sale notice published as two image files (page 1, page 2) is extracted once, from page 1 plus page 2 joined, instead of twice as two unrelated notices.

**Architecture:** A DB-free grouping module finds page groups from the listing's own file order. A script writes the joined text and metadata onto page 1 (the leader) and a pointer onto page 2 (the follower). Every extraction reader and the review API pick the joined text with `coalesce(d.stitched_markdown, d.markdown)` and skip followers with `d.stitched_into IS NULL`. The LangExtract window ceiling rises so the joined text stays in one call.

**Tech Stack:** Python 3.11, Neo4j (Cypher via `api.neo4j_client.run_query` / `run_read_query`), pytest, vanilla JS review pages under `web/`.

**Spec:** `docs/superpowers/specs/2026-09-14-stitch-sibling-pages-design.md`

## Global Constraints

- Leader properties: `stitched_markdown` (string), `stitched_pages` (list of filenames, leader first), `stitched_page_offsets` (list of int), `stitched_at` (datetime), `stitched_expected_lot_count` (int or null).
- Follower property: `stitched_into` (leader filename). Nothing else on a follower changes.
- Pages are joined with exactly `"\n\n"`.
- Read contract everywhere: `coalesce(d.stitched_markdown, d.markdown)` for text, `coalesce(d.stitched_expected_lot_count, d.expected_lot_count)` for the lot count, `d.stitched_into IS NULL` to exclude followers.
- Window ceiling default (`LANGEXTRACT_MAX_CHAR_BUFFER_CEILING`) goes from `30000` to `64000`.
- Stitching or unstitching a Document that already holds `extraction_json` stamps `extraction_stale_at = datetime()` on it.
- DB-writing backfill steps run only after the user's explicit approval, one step at a time.
- Tests are DB-free: Cypher is asserted by capturing the query string, as in `tests/scripts/test_reset_langextract_refresh.py`.
- Run tests with `python -m pytest <path> -q` from the repo root (pytest's `testpaths` only covers `tests/api`, so name the file).
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File map

| File | Responsibility |
|---|---|
| `pipeline/notice_pages.py` (new) | Pure grouping and joining: `page_groups`, `stitch_pages` |
| `scripts/stitch_sibling_pages.py` (new) | Reads candidates from Neo4j, prints the plan, writes/unwrites stitch properties |
| `pipeline/extract_routing.py` | Ceiling default 64000 |
| `pipeline/load_extractions.py` | `_fetch` and read contract |
| `scripts/reset_langextract_and_extract.py` | `select_docs`, `select_stale_docs`, `select_refresh_docs` read contract |
| `pipeline/apply_extractions.py`, `pipeline/promote_extractions.py` | Skip followers |
| `api/review/extraction.py` | `extraction_stale` with the stale stamp, queue/count/detail queries, follower pointer, rerun worker |
| `api/review/queries.py`, `api/review/router.py` | Classification rows carry `stitched_into` / `stitched_pages` |
| `web/review_extraction.html` | Page dividers, follower notice, pages badge |
| `web/review.html` | Classification card "page N of" note |
| `tests/pipeline/test_notice_pages.py` (new), `tests/scripts/test_stitch_sibling_pages.py` (new), plus extensions to existing test files | Coverage per task |

---

### Task 1: Pure grouping and joining (`pipeline/notice_pages.py`)

**Files:**
- Create: `pipeline/notice_pages.py`
- Test: `tests/pipeline/test_notice_pages.py`

**Interfaces:**
- Produces:
  - `page_groups(rows: list[dict]) -> tuple[list[dict], list[dict]]`. Each input row is `{"listing": str, "filename": str, "content_key": str, "position": int | None}` (one row per listing/document edge). Returns `(groups, ambiguous)`. A group is `{"pages": [filenames in page order], "twins": [filenames whose bytes equal one of the pages]}`. An ambiguous entry is `{"filenames": [...], "reason": str}`.
  - `stitch_pages(pages: list[dict]) -> dict`. Each page is `{"filename": str, "markdown": str, "expected_lot_count": int | None}` in order. Returns `{"markdown": str, "offsets": list[int], "expected_lot_count": int | None}`.
  - `SEPARATOR = "\n\n"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/pipeline/test_notice_pages.py
"""Two files, one notice: page groups come from the listing's own file order.

The shapes are the corpus's own (AXIS-1…/AXIS-2… on three listings,
liq-1…/liq-2… on ten). DB-free: every function under test is pure.
"""
from __future__ import annotations

from pipeline.notice_pages import SEPARATOR, page_groups, stitch_pages


def _row(listing, filename, key, position):
    return {"listing": listing, "filename": filename,
            "content_key": key, "position": position}


def _pair(listings, p1="AXIS-1.jpg", p2="AXIS-2.jpg"):
    rows = []
    for a in listings:
        rows.append(_row(a, p1, "sha-1", 0))
        rows.append(_row(a, p2, "sha-2", 1))
    return rows


# ── grouping ────────────────────────────────────────────────────────────────

def test_two_pages_on_the_same_listings_form_one_group_in_portal_order():
    groups, ambiguous = page_groups(_pair(["824034", "824035", "824039"]))
    assert ambiguous == []
    assert groups == [{"pages": ["AXIS-1.jpg", "AXIS-2.jpg"], "twins": []}]


def test_order_follows_position_not_filename():
    rows = [_row("L1", "zzz.jpg", "sha-1", 0), _row("L1", "aaa.jpg", "sha-2", 1)]
    groups, _ = page_groups(rows)
    assert groups[0]["pages"] == ["zzz.jpg", "aaa.jpg"]


def test_a_listing_with_one_document_is_not_a_group():
    groups, ambiguous = page_groups([_row("L1", "only.jpg", "sha-1", 0)])
    assert groups == [] and ambiguous == []


def test_byte_twins_are_not_pages_of_each_other():
    rows = [_row("L1", "a.jpg", "sha-1", 0), _row("L1", "a-copy.jpg", "sha-1", 1)]
    groups, ambiguous = page_groups(rows)
    assert groups == [] and ambiguous == []


def test_a_byte_twin_of_a_page_rides_along_as_a_twin():
    rows = [_row("L1", "p1.jpg", "sha-1", 0), _row("L1", "p1-copy.jpg", "sha-1", 1),
            _row("L1", "p2.jpg", "sha-2", 2)]
    groups, _ = page_groups(rows)
    assert groups == [{"pages": ["p1.jpg", "p2.jpg"], "twins": ["p1-copy.jpg"]}]


def test_pages_must_share_exactly_the_same_listings():
    rows = _pair(["L1", "L2"]) + [_row("L3", "AXIS-1.jpg", "sha-1", 0)]
    groups, ambiguous = page_groups(rows)
    assert groups == []
    assert ambiguous == [{"filenames": ["AXIS-1.jpg", "AXIS-2.jpg"],
                          "reason": "listing sets differ: AXIS-1.jpg is on 3, AXIS-2.jpg is on 2"}]


def test_listings_must_agree_on_the_order():
    rows = [_row("L1", "a.jpg", "sha-1", 0), _row("L1", "b.jpg", "sha-2", 1),
            _row("L2", "a.jpg", "sha-1", 1), _row("L2", "b.jpg", "sha-2", 0)]
    groups, ambiguous = page_groups(rows)
    assert groups == []
    assert ambiguous[0]["reason"] == "order differs across listings"


def test_a_file_missing_from_downloads_list_is_ambiguous():
    rows = [_row("L1", "a.jpg", "sha-1", 0), _row("L1", "b.jpg", "sha-2", None)]
    groups, ambiguous = page_groups(rows)
    assert groups == []
    assert ambiguous == [{"filenames": ["a.jpg", "b.jpg"],
                          "reason": "b.jpg is not in the listing's downloads_list"}]


def test_groups_are_reported_once_however_many_listings_share_them():
    groups, _ = page_groups(_pair([str(n) for n in range(10)], "liq-1.jpg", "liq-2.jpg"))
    assert len(groups) == 1


# ── joining ─────────────────────────────────────────────────────────────────

def test_stitch_joins_in_order_with_the_separator_and_records_offsets():
    out = stitch_pages([{"filename": "p1", "markdown": "AB", "expected_lot_count": 14},
                        {"filename": "p2", "markdown": "CDE", "expected_lot_count": 10}])
    assert out["markdown"] == "AB" + SEPARATOR + "CDE"
    assert out["offsets"] == [0, 2 + len(SEPARATOR)]
    assert out["expected_lot_count"] == 24


def test_stitch_does_not_touch_page_text():
    out = stitch_pages([{"filename": "p1", "markdown": " A \n", "expected_lot_count": 1},
                        {"filename": "p2", "markdown": "\nB", "expected_lot_count": 1}])
    assert out["markdown"] == " A \n" + SEPARATOR + "\nB"


def test_lot_count_is_null_unless_every_page_has_one():
    out = stitch_pages([{"filename": "p1", "markdown": "A", "expected_lot_count": 3},
                        {"filename": "p2", "markdown": "B", "expected_lot_count": None}])
    assert out["expected_lot_count"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/pipeline/test_notice_pages.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.notice_pages'`

- [ ] **Step 3: Write the implementation**

```python
# pipeline/notice_pages.py
"""
pipeline/notice_pages.py
------------------------
Two files, one notice — group the pages so extraction reads them together.

Why this exists
~~~~~~~~~~~~~~~
Some banks publish a sale notice as two images, page 1 and page 2, and the
portal attaches both to every listing the notice covers. Each file is its own
:Document, so page 2 is OCR'd, classified and extracted as a notice of its own —
with no preamble, no bank and no idea it is the tail of a longer list. liq-2…
was counted at 10 lots by a reviewer and extracted as 1.

The portal already knows the order: ``AuctionProperty.downloads_list`` lists
the files as they appear on the listing page, page 1 first. That order, plus
"the same files on the same listings", is the whole detection rule.

Two functions, both pure:

``page_groups(rows)``   — which files are pages of one notice, in what order.
``stitch_pages(pages)`` — the joined text and where each page starts in it.

What this is not
~~~~~~~~~~~~~~~~
Not a twin detector. ``pipeline/notice_twins.py`` groups files holding the
SAME bytes; this groups files holding DIFFERENT bytes that belong together. A
twin of a page is carried along in ``twins`` so the caller can point it at the
leader, but it is never a page of its own.

DB-free on purpose: the caller (``scripts/stitch_sibling_pages.py``) owns the
Cypher; the decisions live here where a unit test can reach them.
"""
from __future__ import annotations

from collections import defaultdict

#: Pages are joined with this. Fixed, because extraction offsets are positions
#: in the joined string and ``stitched_page_offsets`` is derived from it.
SEPARATOR = "\n\n"


def page_groups(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Group listing/document rows into ordered page groups.

    ``rows`` — one per (listing, document) edge:
        ``{"listing", "filename", "content_key", "position"}``
        where ``position`` is the file's index in that listing's
        ``downloads_list`` (None when absent).

    Returns ``(groups, ambiguous)``:

    * group     — ``{"pages": [filenames, page 1 first], "twins": [filenames]}``
                  ``twins`` are files whose bytes equal one of the pages.
    * ambiguous — ``{"filenames": [...], "reason": str}`` for a candidate that
                  failed a check. Nothing is guessed: the reviewer forces or
                  drops it by name.

    A candidate is every listing carrying two or more distinct contents. It
    becomes a group only when every page sits on exactly the same listings and
    every listing lists the pages in the same order.
    """
    by_listing: dict[str, list[dict]] = defaultdict(list)
    doc_listings: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        by_listing[r["listing"]].append(r)
        doc_listings[r["filename"]].add(r["listing"])

    # candidate key (ordered page tuple) -> set of listings proposing it
    candidates: dict[tuple[str, ...], set[str]] = defaultdict(set)
    twins_for: dict[tuple[str, ...], set[str]] = defaultdict(set)
    ambiguous: list[dict] = []
    flagged: set[frozenset] = set()

    def flag(names: list[str], reason: str) -> None:
        key = frozenset(names)
        if key in flagged:
            return
        flagged.add(key)
        ambiguous.append({"filenames": sorted(names), "reason": reason})

    for listing, docs in by_listing.items():
        distinct = {d["content_key"] for d in docs}
        if len(distinct) < 2:
            continue
        ordered = sorted(docs, key=lambda d: (d["position"] is None,
                                              d["position"] if d["position"] is not None else 0,
                                              d["filename"]))
        missing = [d["filename"] for d in ordered if d["position"] is None]
        if missing:
            flag([d["filename"] for d in ordered],
                 f"{missing[0]} is not in the listing's downloads_list")
            continue
        pages: list[str] = []
        twins: list[str] = []
        seen: dict[str, str] = {}
        for d in ordered:
            first = seen.get(d["content_key"])
            if first is None:
                seen[d["content_key"]] = d["filename"]
                pages.append(d["filename"])
            else:
                twins.append(d["filename"])
        key = tuple(pages)
        candidates[key].add(listing)
        twins_for[key].update(twins)

    # the same member set under two orders -> the listings disagree
    by_members: dict[frozenset, list[tuple[str, ...]]] = defaultdict(list)
    for key in candidates:
        by_members[frozenset(key)].append(key)

    groups: list[dict] = []
    for members, keys in by_members.items():
        if len(keys) > 1:
            flag(list(members), "order differs across listings")
            continue
        key = keys[0]
        leader = key[0]
        base = doc_listings[leader]
        bad = next((fn for fn in key[1:] if doc_listings[fn] != base), None)
        if bad is not None:
            flag(list(key), f"listing sets differ: {leader} is on {len(base)}, "
                            f"{bad} is on {len(doc_listings[bad])}")
            continue
        groups.append({"pages": list(key), "twins": sorted(twins_for[key])})
    groups.sort(key=lambda g: g["pages"][0])
    ambiguous.sort(key=lambda a: a["filenames"][0])
    return groups, ambiguous


def stitch_pages(pages: list[dict]) -> dict:
    """Join page texts in order. Returns ``{"markdown", "offsets", "expected_lot_count"}``.

    Page text is used exactly as stored — no strip, no normalisation — because
    a follower's own blocks and highlights still index its own markdown, and a
    reviewer comparing the two must see the same characters.

    ``expected_lot_count`` is the sum of the pages' counts, or None when any
    page has none: a partial sum would tell the model to find fewer lots than
    the notice holds, which is the very bug this module exists to fix.
    """
    parts: list[str] = []
    offsets: list[int] = []
    pos = 0
    for i, p in enumerate(pages):
        if i:
            parts.append(SEPARATOR)
            pos += len(SEPARATOR)
        offsets.append(pos)
        md = p.get("markdown") or ""
        parts.append(md)
        pos += len(md)
    counts = [p.get("expected_lot_count") for p in pages]
    total = sum(int(c) for c in counts) if counts and all(c is not None for c in counts) else None
    return {"markdown": "".join(parts), "offsets": offsets,
            "expected_lot_count": total}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/pipeline/test_notice_pages.py -q`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add pipeline/notice_pages.py tests/pipeline/test_notice_pages.py
git commit -m "pipeline: group the pages of one notice from the listing's file order

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: The stitch script (`scripts/stitch_sibling_pages.py`)

**Files:**
- Create: `scripts/stitch_sibling_pages.py`
- Test: `tests/scripts/test_stitch_sibling_pages.py`

**Interfaces:**
- Consumes: `page_groups`, `stitch_pages`, `SEPARATOR` from Task 1; `run_query`, `run_read_query` from `api.neo4j_client`.
- Produces (module functions, all tested by capturing Cypher):
  - `fetch_rows() -> list[dict]` — listing/document rows for listings holding 2+ documents with markdown.
  - `plan(rows, only: set[str] | None, skip: set[str]) -> tuple[list[dict], list[dict]]` — applies `--only` / `--skip` to `page_groups` output; a `--skip` on any member drops the group; `--only` forces a group whose leader is named even if it was ambiguous (its members are taken from the ambiguous entry's filenames in position order).
  - `build(group, docs_by_name) -> dict` — `{"leader", "followers", "markdown", "offsets", "pages", "expected_lot_count", "changed"}`.
  - `apply_group(built) -> int` — writes leader + followers, returns follower count.
  - `unstitch(leader) -> int` — removes the properties, returns follower count.

- [ ] **Step 1: Write the failing tests**

```python
# tests/scripts/test_stitch_sibling_pages.py
"""What the stitch script writes, and when it refuses to.

The decisions (which files are pages) are tested in
tests/pipeline/test_notice_pages.py. Here the subject is the script's own job:
building the write from stored rows and sending the right Cypher. Neo4j is
stood in for by a recorder, as in test_reset_langextract_refresh.py.
"""
from __future__ import annotations

import scripts.stitch_sibling_pages as S


class _Capture:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.calls = []

    def __call__(self, cypher, params=None, **kw):
        self.calls.append((cypher, params))
        return self.rows

    @property
    def cypher(self):
        return self.calls[-1][0]

    @property
    def params(self):
        return self.calls[-1][1]


def _doc(fn, md, elc=None, stitched_markdown=None, has_extraction=False):
    return {"filename": fn, "markdown": md, "expected_lot_count": elc,
            "stitched_markdown": stitched_markdown,
            "has_extraction": has_extraction}


GROUP = {"pages": ["p1.jpg", "p2.jpg"], "twins": ["p1-copy.jpg"]}
DOCS = {"p1.jpg": _doc("p1.jpg", "PAGE ONE", 14),
        "p2.jpg": _doc("p2.jpg", "PAGE TWO", 10),
        "p1-copy.jpg": _doc("p1-copy.jpg", "PAGE ONE", 14)}


# ── build ───────────────────────────────────────────────────────────────────

def test_build_joins_pages_and_points_twins_at_the_leader():
    b = S.build(GROUP, DOCS)
    assert b["leader"] == "p1.jpg"
    assert b["followers"] == ["p2.jpg", "p1-copy.jpg"]
    assert b["markdown"] == "PAGE ONE" + S.SEPARATOR + "PAGE TWO"
    assert b["offsets"] == [0, len("PAGE ONE") + len(S.SEPARATOR)]
    assert b["pages"] == ["p1.jpg", "p2.jpg"]
    assert b["expected_lot_count"] == 24
    assert b["changed"] is True


def test_build_reports_unchanged_when_the_leader_already_holds_this_text():
    docs = dict(DOCS)
    docs["p1.jpg"] = _doc("p1.jpg", "PAGE ONE", 14,
                          stitched_markdown="PAGE ONE" + S.SEPARATOR + "PAGE TWO")
    assert S.build(GROUP, docs)["changed"] is False


# ── plan (--only / --skip) ──────────────────────────────────────────────────

def _rows():
    return [{"listing": "L1", "filename": "p1.jpg", "content_key": "s1", "position": 0},
            {"listing": "L1", "filename": "p2.jpg", "content_key": "s2", "position": 1},
            {"listing": "L2", "filename": "q1.jpg", "content_key": "s3", "position": 0},
            {"listing": "L2", "filename": "q2.jpg", "content_key": "s4", "position": 1}]


def test_skip_drops_a_group_that_names_any_member():
    groups, _ = S.plan(_rows(), only=None, skip={"q2.jpg"})
    assert [g["pages"] for g in groups] == [["p1.jpg", "p2.jpg"]]


def test_only_keeps_just_the_named_leaders():
    groups, _ = S.plan(_rows(), only={"q1.jpg"}, skip=set())
    assert [g["pages"] for g in groups] == [["q1.jpg", "q2.jpg"]]


def test_only_forces_an_ambiguous_group_by_its_leader():
    rows = _rows() + [{"listing": "L3", "filename": "p1.jpg", "content_key": "s1", "position": 0}]
    groups, ambiguous = S.plan(rows, only={"p1.jpg"}, skip=set())
    assert [g["pages"] for g in groups] == [["p1.jpg", "p2.jpg"]]
    assert ambiguous == []


# ── writes ──────────────────────────────────────────────────────────────────

def test_apply_writes_leader_fields_and_follower_pointer(monkeypatch):
    cap = _Capture(rows=[{"followers": 2}])
    monkeypatch.setattr(S, "run_query", cap)
    n = S.apply_group(S.build(GROUP, DOCS))
    assert n == 2
    c = cap.cypher
    assert "l.stitched_markdown = $md" in c
    assert "l.stitched_pages = $pages" in c
    assert "l.stitched_page_offsets = $offsets" in c
    assert "l.stitched_at = datetime()" in c
    assert "l.stitched_expected_lot_count = $elc" in c
    assert "f.stitched_into = $leader" in c
    assert cap.params["leader"] == "p1.jpg"
    assert cap.params["followers"] == ["p2.jpg", "p1-copy.jpg"]
    assert cap.params["elc"] == 24


def test_apply_stamps_stale_only_where_an_extraction_exists(monkeypatch):
    cap = _Capture(rows=[{"followers": 2}])
    monkeypatch.setattr(S, "run_query", cap)
    S.apply_group(S.build(GROUP, DOCS))
    assert ("l.extraction_stale_at = CASE WHEN l.extraction_json IS NOT NULL "
            "THEN datetime() ELSE l.extraction_stale_at END") in cap.cypher


def test_unstitch_removes_every_stitch_property_and_stamps_stale(monkeypatch):
    cap = _Capture(rows=[{"followers": 1}])
    monkeypatch.setattr(S, "run_query", cap)
    assert S.unstitch("p1.jpg") == 1
    c = cap.cypher
    for prop in ("stitched_markdown", "stitched_pages", "stitched_page_offsets",
                 "stitched_at", "stitched_expected_lot_count"):
        assert f"l.{prop}" in c
    assert "REMOVE f.stitched_into" in c
    assert "f.extraction_stale_at = CASE" in c
    assert "l.extraction_stale_at = CASE" in c
    assert cap.params == {"leader": "p1.jpg"}


def test_dry_run_writes_nothing(monkeypatch, capsys):
    reads = _Capture(rows=[])
    writes = _Capture()
    monkeypatch.setattr(S, "run_read_query", reads)
    monkeypatch.setattr(S, "run_query", writes)
    assert S.main(["--dry-run"]) == 0
    assert writes.calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/scripts/test_stitch_sibling_pages.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.stitch_sibling_pages'`

- [ ] **Step 3: Write the script**

```python
# scripts/stitch_sibling_pages.py
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
        rest: list[dict] = []
        for amb in ambiguous:
            if amb["filenames"][0] in only or any(f in only for f in amb["filenames"]):
                # take the members in their position order on the first listing
                # that carries them all
                pos = {}
                for r in rows:
                    if r["filename"] in amb["filenames"] and r["position"] is not None:
                        pos.setdefault(r["filename"], r["position"])
                ordered = sorted(amb["filenames"], key=lambda f: (pos.get(f, 1 << 30), f))
                forced.append({"pages": ordered, "twins": []})
            else:
                rest.append(amb)
        groups = [g for g in groups + forced if g["pages"][0] in only]
        ambiguous = rest
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/scripts/test_stitch_sibling_pages.py -q`
Expected: 9 passed

- [ ] **Step 5: Live dry run (read-only, no approval needed)**

Run: `python -m scripts.stitch_sibling_pages --dry-run`
Expected: 14 groups printed with `[write]`, the `03d7249a…` / `4a710075…` pair either listed as a group or under `[ambiguous]`, last line `dry run — nothing written`. Paste the output into the commit message body if it differs from 14.

- [ ] **Step 6: Commit**

```bash
git add scripts/stitch_sibling_pages.py tests/scripts/test_stitch_sibling_pages.py
git commit -m "scripts: stitch the pages of a two-file notice onto page 1

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Extraction readers follow the stitch contract

**Files:**
- Modify: `pipeline/load_extractions.py:91-110` (`_fetch`)
- Modify: `scripts/reset_langextract_and_extract.py:105-130` (`select_docs`), `:137-165` (`select_stale_docs`), `:168-245` (`select_refresh_docs`)
- Modify: `api/review/extraction.py:495-517` (`_rerun_worker`)
- Test: `tests/pipeline/test_load_extractions_twins.py` (extend), `tests/scripts/test_reset_langextract_refresh.py` (extend), `tests/api/test_extraction_stitched.py` (new)

**Interfaces:**
- Consumes: Document properties from Task 2.
- Produces: every selector returns `md` as the stitched text when present and `expected_lot_count` as the group count when present, and never returns a follower.

- [ ] **Step 1: Write the failing tests**

Append to `tests/pipeline/test_load_extractions_twins.py`:

```python


# ── stitched pages (pipeline/notice_pages) ──────────────────────────────────

class _Capture:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.cypher = None

    def __call__(self, cypher, params=None, **kw):
        self.cypher = cypher
        return self.rows


def test_fetch_reads_the_stitched_text_and_skips_followers(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(M, "run_read_query", cap)
    M._fetch(None, False, None)
    assert "d.stitched_into IS NULL" in cap.cypher
    assert "coalesce(d.stitched_markdown, d.markdown) AS md" in cap.cypher
    assert ("coalesce(d.stitched_expected_lot_count, d.expected_lot_count) "
            "AS expected_lot_count") in cap.cypher
```

Append to `tests/scripts/test_reset_langextract_refresh.py`:

```python


# ── stitched pages: every selector reads the joined text, never a follower ──

def _selector_cyphers(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(R, "run_read_query", cap)
    out = []
    R.select_docs("2026-01-01", 90, resume=True, limit=None); out.append(cap.cypher)
    R.select_stale_docs(90, limit=None); out.append(cap.cypher)
    R.select_refresh_docs(90, 60, single_lot=False, limit=None); out.append(cap.cypher)
    return out


def test_every_selector_skips_followers(monkeypatch):
    for c in _selector_cyphers(monkeypatch):
        assert "d.stitched_into IS NULL" in c


def test_every_selector_reads_the_stitched_text_and_count(monkeypatch):
    for c in _selector_cyphers(monkeypatch):
        assert "coalesce(d.stitched_markdown, d.markdown) AS md" in c
        assert ("coalesce(d.stitched_expected_lot_count, d.expected_lot_count) "
                "AS expected_lot_count") in c


def test_refresh_treats_a_newer_stitch_as_stale(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(R, "run_read_query", cap)
    R.select_refresh_docs(90, 60, single_lot=False, limit=None)
    assert "toString(d.stitched_at) AS st" in cap.cypher
    assert "OR st > ex" in cap.cypher
```

Create `tests/api/test_extraction_stitched.py`:

```python
"""The review API on a stitched notice: leader shows the joined text, follower
points at the leader, and the stale badge sees the classification stamp."""
from __future__ import annotations

import inspect

from api.review import extraction as E


def test_rerun_worker_reads_the_stitched_text_and_refuses_followers():
    src = inspect.getsource(E._rerun_worker)
    assert "coalesce(d.stitched_markdown, d.markdown) AS md" in src
    assert "d.stitched_into AS stitched_into" in src
    assert "stitched into" in src   # the refusal message
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/pipeline/test_load_extractions_twins.py tests/scripts/test_reset_langextract_refresh.py tests/api/test_extraction_stitched.py -q`
Expected: 5 failures (`assert "d.stitched_into IS NULL" in ...`), everything else passes

- [ ] **Step 3: Update `_fetch` in `pipeline/load_extractions.py`**

Replace the body of `_fetch`:

```python
def _fetch(limit: int | None, force: bool, filename: str | None) -> list[dict]:
    # A follower (page 2 of a stitched notice, pipeline/notice_pages) is never
    # extracted on its own: its text rides in the leader's stitched_markdown.
    where = ("d.markdown IS NOT NULL AND d.markdown <> '' "
             "AND d.stitched_into IS NULL")
    if not force:
        where += " AND d.extraction_json IS NULL"
    if filename:
        where += " AND d.filename = $fn"
    return run_read_query(
        f"MATCH (d:Document) WHERE {where} "
        + ROSTER_CYPHER +
        "RETURN d.filename AS filename, "
        "       coalesce(d.stitched_markdown, d.markdown) AS md, "
        "       d.notice_type AS notice_type, "
        "       coalesce(d.stitched_expected_lot_count, d.expected_lot_count) "
        "         AS expected_lot_count, "
        "       roster AS roster "
        "ORDER BY d.filename"
        + (f" LIMIT {int(limit)}" if limit else ""),
        {"fn": filename} if filename else None,
        max_rows=20_000, timeout=120.0)
```

- [ ] **Step 4: Update the three selectors in `scripts/reset_langextract_and_extract.py`**

In `select_docs`, change the `where` list and the RETURN:

```python
    where = [
        "d.markdown IS NOT NULL AND d.markdown <> ''",
        "d.stitched_into IS NULL",
        "d.ocr_health_score > $min_ocr",
        "a.auction_start_dt >= datetime($since)",
    ]
```
and
```python
        "RETURN d.filename AS filename, "
        "       coalesce(d.stitched_markdown, d.markdown) AS md, "
        "       d.notice_type AS notice_type, "
        "       coalesce(d.stitched_expected_lot_count, d.expected_lot_count) "
        "         AS expected_lot_count, "
        "       roster AS roster "
```

In `select_stale_docs`, the query becomes:

```python
    q = (
        "MATCH (d:Document) "
        "WHERE d.extraction_stale_at IS NOT NULL "
        "  AND d.markdown IS NOT NULL AND d.markdown <> '' "
        "  AND d.stitched_into IS NULL "
        "  AND d.ocr_health_score > $min_ocr "
        + ROSTER_CYPHER +
        "RETURN d.filename AS filename, "
        "       coalesce(d.stitched_markdown, d.markdown) AS md, "
        "       d.notice_type AS notice_type, "
        "       coalesce(d.stitched_expected_lot_count, d.expected_lot_count) "
        "         AS expected_lot_count, "
        "       roster AS roster "
        "ORDER BY d.filename"
        + (f" LIMIT {int(limit)}" if limit else "")
    )
```

In `select_refresh_docs`, a newer stitch is a third timestamp signal:

```python
    stale_when = "md > ex OR st > ex OR d.extraction_score < $min_score"
```
and the query:
```python
    q = (
        "MATCH (d:Document) "
        "WHERE d.extraction_json IS NOT NULL "
        "  AND d.markdown IS NOT NULL AND d.markdown <> '' "
        "  AND d.stitched_into IS NULL "
        "  AND d.ocr_health_score > $min_ocr "
        + lot_filter +
        "WITH d, toString(d.extraction_at) AS ex, "
        "     toString(coalesce(d.markdown_raw_at, d.markdown_loaded_at)) AS md, "
        "     toString(d.stitched_at) AS st "
        f"WHERE {stale_when} "
        + ROSTER_CYPHER +
        "RETURN d.filename AS filename, "
        "       coalesce(d.stitched_markdown, d.markdown) AS md, "
        "       d.notice_type AS notice_type, "
        "       coalesce(d.stitched_expected_lot_count, d.expected_lot_count) "
        "         AS expected_lot_count, "
        "       roster AS roster "
        "ORDER BY d.filename"
        + (f" LIMIT {int(limit)}" if limit else "")
    )
```

Check the existing test `test_refresh_selects_on_both_staleness_signals` asserts the string `"md > ex OR d.extraction_score < $min_score"`; update that assertion to `"md > ex OR st > ex OR d.extraction_score < $min_score"`.

- [ ] **Step 5: Update `_rerun_worker` in `api/review/extraction.py`**

```python
        rows = run_read_query(
            "MATCH (d:Document {filename: $fn}) "
            "RETURN d.filename AS filename, "
            "       coalesce(d.stitched_markdown, d.markdown) AS md, "
            "       d.notice_type AS notice_type, "
            "       coalesce(d.stitched_expected_lot_count, d.expected_lot_count) "
            "         AS expected_lot_count, "
            "       d.stitched_into AS stitched_into",
            {"fn": filename})
        if rows and rows[0].get("stitched_into"):
            raise RuntimeError(f"this page is stitched into {rows[0]['stitched_into']}; "
                               "re-run that document instead")
        if not rows or not (rows[0].get("md") or "").strip():
            raise RuntimeError("document has no markdown to extract from")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/pipeline/test_load_extractions_twins.py tests/scripts/test_reset_langextract_refresh.py tests/api/test_extraction_stitched.py -q`
Expected: all passed

- [ ] **Step 7: Commit**

```bash
git add pipeline/load_extractions.py scripts/reset_langextract_and_extract.py api/review/extraction.py tests/pipeline/test_load_extractions_twins.py tests/scripts/test_reset_langextract_refresh.py tests/api/test_extraction_stitched.py
git commit -m "extract: read a stitched notice from its leader, never its follower

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Raise the window ceiling to 64000

**Files:**
- Modify: `pipeline/extract_routing.py:25-45`
- Modify: `tests/api/test_extract_model_routing.py` (add one test)
- Modify: `tests/pipeline/test_lot_windows.py:19` (pin the ceiling the fixtures assume)

**Interfaces:**
- Produces: `char_buffer_for(markdown)` returns up to 64000 by default.

- [ ] **Step 1: Write the failing test**

Append to `tests/api/test_extract_model_routing.py`:

```python


def test_char_buffer_default_ceiling_holds_a_stitched_pair(monkeypatch):
    """The largest two-page notice joins to 39k chars; at the old 30k ceiling
    LangExtract would cut the stitch straight back into two windows."""
    monkeypatch.delenv("LANGEXTRACT_MAX_CHAR_BUFFER", raising=False)
    monkeypatch.delenv("LANGEXTRACT_MAX_CHAR_BUFFER_CEILING", raising=False)
    assert er.char_buffer_for("x" * 39000) == 39000
    assert er.char_buffer_for("x" * 90000) == 64000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/api/test_extract_model_routing.py -q -k default_ceiling`
Expected: FAIL, `assert 30000 == 39000`

- [ ] **Step 3: Change the default and its docstring**

In `pipeline/extract_routing.py`, in `char_buffer_for`:

```python
      base (LANGEXTRACT_MAX_CHAR_BUFFER, default 4000): floor for small notices.
      ceil (LANGEXTRACT_MAX_CHAR_BUFFER_CEILING, default 64000): cap so a
           pathologically long bundle still splits instead of one giant call.
           64000 holds the largest stitched two-page notice (39k chars,
           pipeline/notice_pages) and the big single-page bundles that were
           being cut into two windows at 30000.
```
and
```python
    if ceil is None:
        ceil = int(os.environ.get("LANGEXTRACT_MAX_CHAR_BUFFER_CEILING", "64000"))
```

- [ ] **Step 4: Pin the lot_windows fixtures to the ceiling they were built for**

In `tests/pipeline/test_lot_windows.py`, right after `BUFFER = 30000 ...`, add:

```python
import pytest


@pytest.fixture(autouse=True)
def _pin_window_ceiling(monkeypatch):
    """These fixtures place entities around multiples of a 30000-char window.
    The production default is now larger (64000); the repair logic under test
    is the same at any ceiling, so pin the one the offsets were written for."""
    monkeypatch.setenv("LANGEXTRACT_MAX_CHAR_BUFFER_CEILING", str(BUFFER))
```

Also update the module docstring line `(30000 for anything long)` to `(BUFFER, pinned by a fixture below)`.

- [ ] **Step 5: Run the affected tests**

Run: `python -m pytest tests/api/test_extract_model_routing.py tests/pipeline/test_lot_windows.py -q`
Expected: all passed

- [ ] **Step 6: Commit**

```bash
git add pipeline/extract_routing.py tests/api/test_extract_model_routing.py tests/pipeline/test_lot_windows.py
git commit -m "extract: one window up to 64k chars, so a stitched notice is read whole

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Apply and promote skip followers

**Files:**
- Modify: `pipeline/apply_extractions.py:972-996` (`fetch_work`)
- Modify: `pipeline/promote_extractions.py:523-532` (`_FETCH`)
- Test: `tests/pipeline/test_stitched_followers_skipped.py` (new)

**Interfaces:**
- Produces: neither pass reads a Document with `stitched_into`.

- [ ] **Step 1: Write the failing test**

```python
# tests/pipeline/test_stitched_followers_skipped.py
"""A follower (page 2 of a stitched notice) holds no extraction of its own that
apply or promote should act on: its lots live on the leader. Source-level
guards on the two fetch queries."""
from __future__ import annotations

import inspect

import pipeline.apply_extractions as AX
import pipeline.promote_extractions as P


def test_apply_fetch_skips_followers():
    assert "d.stitched_into IS NULL" in inspect.getsource(AX.fetch_work)


def test_promote_fetch_skips_followers():
    assert "d.stitched_into IS NULL" in P._FETCH
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/pipeline/test_stitched_followers_skipped.py -q`
Expected: 2 failed

- [ ] **Step 3: Add the clause to both queries**

`pipeline/apply_extractions.py`, in `fetch_work`:

```python
        "MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document) "
        "WHERE d.extraction_json IS NOT NULL "
        # page 2 of a stitched notice: its lots are on the leader
        "  AND d.stitched_into IS NULL "
        + ("AND d.filename IN $filenames " if filenames is not None else "")
```

`pipeline/promote_extractions.py`:

```python
_FETCH = """
MATCH (d:Document)
WHERE d.extraction_json IS NOT NULL
  AND d.stitched_into IS NULL
  {filename_clause}
RETURN d.filename AS filename,
```

- [ ] **Step 4: Run the test and the two modules' existing tests**

Run: `python -m pytest tests/pipeline/test_stitched_followers_skipped.py tests/pipeline -q -k "apply or promote or stitched"`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add pipeline/apply_extractions.py pipeline/promote_extractions.py tests/pipeline/test_stitched_followers_skipped.py
git commit -m "pipeline: apply and promote leave a stitched follower to its leader

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Review API — stale stamp, stitched text, follower pointer

**Files:**
- Modify: `api/review/extraction.py:62-90` (`ExtractionReviewOut`), `:135-148` (`extraction_stale`), `:152-176` (`get_extraction`), `:178-205` (`_extraction_filter_clause`), `:240-260` (queue query), `:612-640` (queue row build), `:650-673` (`extraction_detail`)
- Test: `tests/api/test_extraction_stitched.py` (extend), `tests/api/test_extraction_stale_on_classify.py` (extend)

**Interfaces:**
- Produces:
  - `extraction_stale(md_reextracted_at, md_loaded_at, extraction_at, extraction_stale_at=None) -> bool`
  - `get_stitch_pointer(filename: str) -> str | None`
  - `ExtractionReviewOut` gains `stitched_into: str | None`, `stitched_pages: list[str]`, `stitched_page_offsets: list[int]`.
  - `ExtractionQueueRow` gains `stitched_pages: int` (page count, 1 for an unstitched row).

- [ ] **Step 1: Write the failing tests**

Append to `tests/api/test_extraction_stitched.py`:

```python


# ── stale badge sees the classification / stitch stamp ──────────────────────

def test_stale_when_the_stamp_is_newer_than_the_extraction():
    assert E.extraction_stale(None, None, "2026-09-05T17:43:51Z",
                              extraction_stale_at="2026-09-05T18:10:17Z") is True


def test_not_stale_when_the_stamp_is_older_than_the_extraction():
    assert E.extraction_stale(None, None, "2026-09-05T18:20:00Z",
                              extraction_stale_at="2026-09-05T18:10:17Z") is False


def test_stamp_is_ignored_without_an_extraction_time():
    assert E.extraction_stale(None, None, None,
                              extraction_stale_at="2026-09-05T18:10:17Z") is False


# ── queries ─────────────────────────────────────────────────────────────────

def test_queue_filter_excludes_followers():
    assert "d.stitched_into IS NULL" in E._extraction_filter_clause(
        None, None, None, None, None, None, None)


def test_queue_query_returns_the_stamp_and_the_group_count():
    src = inspect.getsource(E.list_extraction_queue)
    assert "toString(d.extraction_stale_at) AS extraction_stale_at" in src
    assert ("coalesce(d.stitched_expected_lot_count, d.expected_lot_count) "
            "AS expected_lot_count") in src
    assert "coalesce(d.stitched_pages, [d.filename]) AS stitched_pages" in src


def test_detail_query_returns_the_stitched_text_and_layout():
    src = inspect.getsource(E.get_extraction)
    # substring checks only — the query aligns its AS columns with padding
    assert "coalesce(d.stitched_markdown, d.markdown)" in src
    assert "d.stitched_pages" in src and "d.stitched_page_offsets" in src
    assert "d.stitched_into" in src
    assert "toString(d.extraction_stale_at)" in src


# ── follower detail ─────────────────────────────────────────────────────────

def test_follower_detail_points_at_the_leader(monkeypatch):
    monkeypatch.setattr(E, "get_extraction", lambda fn: None)
    monkeypatch.setattr(E, "get_stitch_pointer", lambda fn: "AXIS-1.jpg")
    out = E.extraction_detail("AXIS-2.jpg", _admin=object())
    assert out.stitched_into == "AXIS-1.jpg"
    assert out.fields == []


def test_follower_with_an_old_extraction_still_points_at_the_leader(monkeypatch):
    row = {"filename": "AXIS-2.jpg", "markdown": "old", "extraction_json": "[]",
           "corrections_json": "{}", "status": "verified", "stitched_into": "AXIS-1.jpg"}
    monkeypatch.setattr(E, "get_extraction", lambda fn: row)
    out = E.extraction_detail("AXIS-2.jpg", _admin=object())
    assert out.stitched_into == "AXIS-1.jpg"
    assert out.fields == []


def test_leader_detail_carries_pages_offsets_and_stale(monkeypatch):
    row = {"filename": "AXIS-1.jpg", "markdown": "P1\n\nP2", "extraction_json": "[]",
           "corrections_json": "{}", "status": "pending", "stitched_into": None,
           "stitched_pages": ["AXIS-1.jpg", "AXIS-2.jpg"],
           "stitched_page_offsets": [0, 4],
           "extraction_at": "2026-09-05T17:43:51Z",
           "extraction_stale_at": "2026-09-05T18:10:17Z"}
    monkeypatch.setattr(E, "get_extraction", lambda fn: row)
    out = E.extraction_detail("AXIS-1.jpg", _admin=object())
    assert out.stitched_pages == ["AXIS-1.jpg", "AXIS-2.jpg"]
    assert out.stitched_page_offsets == [0, 4]
    assert out.stale is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/api/test_extraction_stitched.py -q`
Expected: failures on `extraction_stale` (unexpected keyword), the query strings, `get_stitch_pointer` missing

- [ ] **Step 3: Extend the models**

In `ExtractionReviewOut` add after `rerun_error`:

```python
    # Stitched notices (pipeline/notice_pages). A follower — page 2 of a
    # two-file notice — carries only stitched_into and no fields: its text and
    # lots are reviewed on the leader. A leader lists its pages and where each
    # starts in `markdown`, so the source pane can draw a divider.
    stitched_into: str | None = None
    stitched_pages: list[str] = []
    stitched_page_offsets: list[int] = []
```

In `ExtractionQueueRow` add after `lot_count_mismatch`:

```python
    # How many source files this row's text was stitched from (1 = a plain
    # single-file notice).
    stitched_pages: int = 1
```

- [ ] **Step 4: Extend `extraction_stale`**

```python
def extraction_stale(md_reextracted_at: str | None, md_loaded_at: str | None,
                     extraction_at: str | None,
                     extraction_stale_at: str | None = None) -> bool:
    """True when the stored extraction no longer matches its inputs.

    Two signals. The markdown changed after LangExtract last ran on it —
    re-ingest stamps one of two markers depending on the path (a full MinerU
    re-ingest sets ``markdown_loaded_at``, a single-block re-OCR sets
    ``markdown_reextracted_at``). Or something upstream said so explicitly:
    ``extraction_stale_at`` is stamped when the classification (lot count or
    notice type) changes, when a missing region is recovered, or when pages
    are stitched — the prompt or the text the extraction came from is gone.
    Stale when ANY of the three is newer than ``extraction_at``. All are Neo4j
    datetimes rendered as ISO-8601 UTC strings, which compare correctly
    lexicographically. Unknown extraction time (legacy rows) -> not stale."""
    if not extraction_at:
        return False
    return any(t and t > extraction_at
               for t in (md_reextracted_at, md_loaded_at, extraction_stale_at))
```

- [ ] **Step 5: Extend `get_extraction` and add `get_stitch_pointer`**

```python
def get_extraction(filename: str) -> dict | None:
    rows = run_read_query(
        """
        MATCH (d:Document {filename: $fn})
        WHERE d.extraction_json IS NOT NULL
        RETURN d.filename                                   AS filename,
               coalesce(d.stitched_markdown, d.markdown)    AS markdown,
               d.extraction_json                            AS extraction_json,
               coalesce(d.extraction_corrections_json, '{}') AS corrections_json,
               coalesce(d.extraction_review_status, 'pending') AS status,
               d.extraction_score                           AS score,
               d.extraction_verified_by                     AS verified_by,
               toString(d.extraction_verified_at)           AS verified_at,
               d.public_url                                 AS public_url,
               d.doc_type                                   AS doc_type,
               d.content_type                               AS content_type,
               toString(d.extraction_at)                    AS extraction_at,
               toString(d.markdown_reextracted_at)          AS markdown_reextracted_at,
               toString(d.markdown_loaded_at)               AS markdown_loaded_at,
               toString(d.extraction_stale_at)              AS extraction_stale_at,
               d.stitched_into                              AS stitched_into,
               coalesce(d.stitched_pages, [])               AS stitched_pages,
               coalesce(d.stitched_page_offsets, [])        AS stitched_page_offsets
        LIMIT 1
        """,
        {"fn": filename},
    )
    return rows[0] if rows else None


def get_stitch_pointer(filename: str) -> str | None:
    """The leader a follower page was stitched into, or None."""
    rows = run_read_query(
        "MATCH (d:Document {filename: $fn}) RETURN d.stitched_into AS leader",
        {"fn": filename})
    return (rows[0].get("leader") if rows else None) or None
```

- [ ] **Step 6: Exclude followers in the shared filter clause**

At the start of `_extraction_filter_clause`, change the first assignment to:

```python
    # A follower (page 2 of a stitched notice) is reviewed on its leader.
    clause = "AND d.stitched_into IS NULL"
    clause += " AND coalesce(d.extraction_review_status,'pending') = $status" if status else ""
```

- [ ] **Step 7: Extend the queue query and row build**

In `list_extraction_queue`'s RETURN, replace the `expected_lot_count` line and add two:

```python
               toString(d.extraction_stale_at) AS extraction_stale_at,
               coalesce(d.stitched_expected_lot_count, d.expected_lot_count) AS expected_lot_count,
               coalesce(d.stitched_pages, [d.filename]) AS stitched_pages,
```

In the queue row construction (the `ExtractionQueueRow(...)` call in the list endpoint), change the `stale=` argument and add `stitched_pages`:

```python
            stale=extraction_stale(r.get("markdown_reextracted_at"),
                                   r.get("markdown_loaded_at"),
                                   r.get("extraction_at"),
                                   r.get("extraction_stale_at")),
            stitched_pages=len(r.get("stitched_pages") or [r["filename"]]),
```

- [ ] **Step 8: Rewrite `extraction_detail`**

```python
@router.get("/{filename:path}", response_model=ExtractionReviewOut)
def extraction_detail(
    filename: str,
    _admin: UserOut = Depends(get_current_admin),
) -> ExtractionReviewOut:
    row = get_extraction(filename)
    # Page 2 of a stitched notice: whatever it holds is reviewed on the leader.
    leader = (row or {}).get("stitched_into") or (get_stitch_pointer(filename) if row is None else None)
    if leader:
        return ExtractionReviewOut(filename=filename, stitched_into=leader,
                                   status="pending", fields=[])
    if row is None:
        raise HTTPException(status_code=404, detail="extraction not found")
    running, error = _rerun_state(filename)
    stale = extraction_stale(row.get("markdown_reextracted_at"),
                             row.get("markdown_loaded_at"),
                             row.get("extraction_at"),
                             row.get("extraction_stale_at"))
    return ExtractionReviewOut(
        filename=row["filename"], markdown=row.get("markdown"),
        status=row.get("status", "pending"), score=row.get("score"),
        verified_by=row.get("verified_by"), verified_at=row.get("verified_at"),
        public_url=row.get("public_url"), doc_type=row.get("doc_type"),
        content_type=row.get("content_type"),
        stale=stale,
        rerun_running=running, rerun_error=error,
        stitched_pages=list(row.get("stitched_pages") or []),
        stitched_page_offsets=[int(o) for o in (row.get("stitched_page_offsets") or [])],
        fields=_build_fields(row["extraction_json"], row["corrections_json"],
                             row.get("markdown"), stale),
    )
```

Also in `extraction_rerun`, before starting the thread, refuse a follower:

```python
    if get_stitch_pointer(filename):
        raise HTTPException(status_code=409,
                            detail="this page is stitched into another document; re-run that one")
```

- [ ] **Step 9: Run the API tests**

Run: `python -m pytest tests/api -q`
Expected: all passed (the existing detail tests still pass because every new field has a default)

- [ ] **Step 10: Commit**

```bash
git add api/review/extraction.py tests/api/test_extraction_stitched.py
git commit -m "review: a stitched notice is reviewed on its leader; stale badge reads the stamp

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Classification rows carry the stitch

**Files:**
- Modify: `api/review/queries.py:388-400` (classification queue RETURN)
- Modify: `api/review/router.py:106-119` (`ClassificationRow`)
- Test: `tests/api/test_review_classification.py` (extend)

**Interfaces:**
- Produces: `ClassificationRow.stitched_into: str | None`, `ClassificationRow.stitched_pages: list[str]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/api/test_review_classification.py`:

```python


def test_classification_rows_say_which_page_of_which_notice_they_are():
    import inspect
    from api.review import queries as Q
    from api.review.router import ClassificationRow
    src = inspect.getsource(Q.list_classification_queue)
    assert "AS stitched_into" in src
    assert "AS stitched_pages" in src
    row = ClassificationRow(filename="x")
    assert row.stitched_into is None and row.stitched_pages == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/api/test_review_classification.py -q -k which_page`
Expected: FAIL on the first assert

- [ ] **Step 3: Add the two columns and fields**

In `api/review/queries.py`, in `list_classification_queue`'s RETURN, after the `auction_id_count` line add:

```python
               size(auction_ids)                AS auction_id_count,
               d.stitched_into                  AS stitched_into,
               coalesce(d.stitched_pages, [])   AS stitched_pages
```
(mind the comma on the previous line).

In `api/review/router.py`, in `ClassificationRow` after `auction_id_count`:

```python
    # Stitched notices (pipeline/notice_pages): a follower names its leader; a
    # leader lists its pages. Reviewers keep counting lots per page — the
    # stitch script sums them — but should not classify page 2 as a notice.
    stitched_into: str | None = None
    stitched_pages: list[str] = []
```

- [ ] **Step 4: Run the test**

Run: `python -m pytest tests/api/test_review_classification.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add api/review/queries.py api/review/router.py tests/api/test_review_classification.py
git commit -m "review: classification rows name the leader a stitched page belongs to

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Review UI — dividers, follower notice, page badges

**Files:**
- Modify: `web/review_extraction.html:330-340` (queue row), `:350-360` (header), `:378-386` (source pane), `:441-463` (`srcHtml`)
- Modify: `web/review.html:2774-2780` (classification card badges)

No unit tests exist for these pages; verification is by opening the page (Step 5).

- [ ] **Step 1: Queue row and header badges in `web/review_extraction.html`**

In the queue row builder, after `const staleTag=...;` add:

```js
    const pagesTag=row.stitched_pages>1?` · <span class="badge" style="background:#6366f1;color:#fff" title="Text stitched from ${row.stitched_pages} source files (page 1 + page 2)">${row.stitched_pages} pages</span>`:"";
```
and include `${pagesTag}` right after `${staleTag}` in `e.innerHTML`.

In `render()`, in the `status` innerHTML chain, add after the `d.stale` term:

```js
    (d.stitched_pages&&d.stitched_pages.length>1?` <span class="badge" style="background:#6366f1;color:#fff" title="${esc(d.stitched_pages.join(' + '))}">stitched: ${d.stitched_pages.length} pages</span>`:"")+
```

- [ ] **Step 2: Follower notice in `render()`**

Right after `const d=CUR;` add:

```js
  if(d.stitched_into){
    document.getElementById("title").textContent=d.filename;
    document.getElementById("status").innerHTML=`<span class="badge" style="background:#6366f1;color:#fff">page of a stitched notice</span>`;
    srcEl.innerHTML=`<div style="padding:16px;color:#334155">This file is a later page of <b>${esc(d.stitched_into)}</b>. Its text and lots are reviewed there.<br><br><a href="#" onclick="loadDoc(${JSON.stringify(d.stitched_into)});return false;">Open ${esc(d.stitched_into)} →</a></div>`;
    fldEl.innerHTML="";
    const rb=document.getElementById("rerunBtn");if(rb)rb.disabled=true;
    return;
  }
```
`srcEl` and `fldEl` are defined on the line `const srcEl=document.getElementById("src"),fldEl=document.getElementById("fields");` a few lines below `const d=CUR;` — move that line up so it sits directly after `const d=CUR;`, before this block.

- [ ] **Step 3: Page dividers in the source pane**

In `render()`, after `for(const x of sp){...}` builds `use`, add:

```js
  // page dividers for a stitched notice: zero-width pseudo-spans at each page start
  const brks=(d.stitched_page_offsets||[]).slice(1).map((o,i)=>({s:o,e:o,c:"__page__",id:null,n:i+2,fn:(d.stitched_pages||[])[i+1]||""}));
  const useAll=use.concat(brks).sort((a,b)=>a.s-b.s);
```
and change `srcEl.innerHTML=srcHtml(md,use,rendered);` to `srcEl.innerHTML=srcHtml(md,useAll,rendered);`.

In `srcHtml`, the plain path becomes:

```js
  let out="",pos=0;
  for(const x of use){out+=esc(md.slice(pos,x.s));
    if(x.c==="__page__"){out+=pageBreak(x);pos=x.s;continue;}
    out+=`<mark data-fid="${x.id}" style="background:${COLORS[x.c]||'#e2e8f0'}" title="${x.c}">${esc(md.slice(x.s,x.e))}</mark>`;pos=x.e;}
  return out+esc(md.slice(pos));
```

and the rendered path's loop and replace become:

```js
    for(let i=use.length-1;i>=0;i--){const x=use[i];
      const a=Math.max(0,Math.min(x.s,s.length)),b=Math.max(a,Math.min(x.e,s.length));
      if(x.c==="__page__"){s=s.slice(0,a)+OPEN(i)+s.slice(a);continue;}
      const region=s.slice(a,b).split("\n").join(M1+"\n"+OPEN(i));
      s=s.slice(0,a)+OPEN(i)+region+M1+s.slice(b);}
    let html=window.DOMPurify.sanitize(window.marked.parse(s,{gfm:true,breaks:false}));
    const reOpen=new RegExp(M0+"(\\d+)"+M2,"g");
    return html.replace(reOpen,(m,n)=>{const x=use[+n];
        if(x.c==="__page__")return pageBreak(x);
        return `<mark data-fid="${x.id}" style="background:${COLORS[x.c]||'#e2e8f0'}" title="${x.c}">`;})
      .split(M1).join("</mark>");
```

Add the helper next to `srcHtml`:

```js
function pageBreak(x){
  return `<span class="pgbrk" title="${esc(x.fn)}">— page ${x.n}: ${esc(x.fn)} —</span>`;
}
```

Add to the page's `<style>`:

```css
  .pgbrk{display:block;margin:14px 0;padding:4px 8px;border-top:2px dashed #6366f1;color:#6366f1;font-size:12px;font-family:'IBM Plex Mono',monospace}
```

- [ ] **Step 4: Classification card note in `web/review.html`**

After `const lotCountBadge = ...;` (around line 2775) add:

```js
    const stitchBadge = n.stitched_into
      ? `<span class="pill warn" title="This file is a later page of ${escapeHtml(n.stitched_into)}; its lots are extracted there. Count this page's lots here — the stitch sums the pages.">page of ${escapeHtml(n.stitched_into)}</span>`
      : (n.stitched_pages && n.stitched_pages.length > 1
        ? `<span class="pill on" title="${escapeHtml(n.stitched_pages.join(' + '))}">stitched: ${n.stitched_pages.length} pages</span>`
        : '');
```
and add `${stitchBadge}` to the `crow` div after `${lotCountBadge}`.

- [ ] **Step 5: Verify in the browser**

Start the API and open the two pages against the live graph (no stitch has been applied yet, so the check is "nothing changed for unstitched rows"):

Run: `python -m uvicorn api.main:app --port 8000` then open `http://localhost:8000/review_extraction.html` and `http://localhost:8000/review.html`.
Expected: queue renders, no console errors, opening a document shows fields and highlights as before, no `pages` badge on any row. Take one screenshot of each page for the PR.

- [ ] **Step 6: Commit**

```bash
git add web/review_extraction.html web/review.html
git commit -m "review ui: page dividers on a stitched notice, follower points at its leader

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Full test run and PR

**Files:** none new.

- [ ] **Step 1: Run every test directory**

Run: `python -m pytest tests/api tests/pipeline tests/scripts -q`
Expected: all passed. Paste the summary line into the PR body.

- [ ] **Step 2: Open the PR**

Use `/ship` per the project's routing rules, or:

```bash
git push -u origin claude/langextraction-single-lot-0a8ce3
gh pr create --title "Stitch the pages of a two-file sale notice before extraction" --body-file - <<'EOF'
Spec: docs/superpowers/specs/2026-09-14-stitch-sibling-pages-design.md
Plan: docs/superpowers/plans/2026-09-14-stitch-sibling-pages.md

- pipeline/notice_pages: page groups from the listing's own file order
- scripts/stitch_sibling_pages: dry-run / --apply / --only / --skip / --unstitch
- every extraction reader uses coalesce(stitched_markdown, markdown) and skips followers
- window ceiling 30k -> 64k so a stitched pair is one call
- review API + UI: leader shows page dividers, follower points at its leader, stale badge reads extraction_stale_at
- classification rows/cards name the leader a page belongs to

Tests: <paste summary line>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
```

---

### Task 10: Backfill runbook (each step needs the user's explicit go-ahead)

**Files:** none. This is the sequence to run after the PR merges. Do not run any `--apply`, `--stale` or promote step without the user saying yes to that specific step.

- [ ] **Step 1: Dry run and review**

```bash
python -m scripts.stitch_sibling_pages --dry-run
```
Expected: 14 `[write]` groups. Show the user the list plus the ambiguous entries and ask which ambiguous ones to force with `--only` and which groups to `--skip`.

- [ ] **Step 2: Apply (writes 14 leaders + 14 followers, clears the followers' old lots)**

```bash
python -m scripts.stitch_sibling_pages --apply
```
Verify:
```cypher
MATCH (d:Document) WHERE d.stitched_into IS NOT NULL RETURN count(d)   // 14
MATCH (d:Document) WHERE d.stitched_markdown IS NOT NULL
RETURN d.filename, size(d.stitched_markdown), d.stitched_expected_lot_count, d.extraction_stale_at IS NOT NULL
MATCH (d:Document) WHERE d.stitched_into IS NOT NULL MATCH (d)-[:HAS_LOT]->(l) RETURN count(l)   // 0 unless --keep-follower-lots was used
```

- [ ] **Step 3: Re-extract stale rows (the 14 leaders + the 8 classification-stale rows)**

Note: after recounting lots on any stitched page, re-run `python -m scripts.stitch_sibling_pages --apply` before `--stale`, so the leader's `stitched_expected_lot_count` takes the new sum and the leader is stamped stale.

```bash
python -m scripts.reset_langextract_and_extract --stale --count-only
python -m scripts.reset_langextract_and_extract --stale --concurrency 8
```

- [ ] **Step 4: Re-extract the notices the old 30k window had cut**

Take the ceiling change's commit time (`git log -1 --format=%cI -- pipeline/extract_routing.py`) as `<T>`:

```bash
python -m scripts.reset_langextract_and_extract --refresh --extracted-before <T> --min-chars 30000 --count-only
```
Expected around 20 (notices over 30k chars; `--min-chars 30000` keeps the run from re-extracting the whole corpus). Then run the same command without `--count-only`.

- [ ] **Step 5: Apply and promote, then check the lot badge**

Promote first (lots and parcels), then apply (fields onto listings), the order the README's pipeline table gives (steps 7 and 8):

```bash
python -m pipeline.promote_extractions --workers 8
python -m pipeline.apply_extractions
```

Then:

```cypher
MATCH (d:Document) WHERE d.stitched_markdown IS NOT NULL
WITH d, [x IN apoc.convert.fromJsonList(d.extraction_json)
         WHERE x.attrs.lot_index IS NOT NULL | toString(x.attrs.lot_index)] AS idx
RETURN d.filename, d.stitched_expected_lot_count AS expected, size(apoc.coll.toSet(idx)) AS extracted
```
Expected: `expected = extracted` on the leaders. Report any mismatch to the user with the filename, before and after counts.
