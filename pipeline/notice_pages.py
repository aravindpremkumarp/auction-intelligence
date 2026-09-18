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

Three functions, all pure:

``page_groups(rows)``   — which files are pages of one notice, in what order.
``order_pages_by_cues`` — that order re-checked against the pages' own
                          continuation lines, because the portal's file order
                          is reversed on at least one live listing.
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

import re
from collections import defaultdict

#: Pages are joined with this. Fixed, because extraction offsets are positions
#: in the joined string and ``stitched_page_offsets`` is derived from it.
SEPARATOR = "\n\n"

#: Reason prefix for a byte twin left out of its group because it sits on a
#: listing set other than the leader's. Such an entry is a report, never a
#: group ``--only`` may force: its two files hold the same text.
TWIN_OUTSIDE_GROUP = "twin outside group"

#: Reason prefix for a group whose pages carry continuation lines that no
#: order satisfies. Reported, never guessed at.
CUE_CONFLICT = "continuation lines contradict the file order"

#: The notice's own words about where a page sits. Banks print one or both
#: across a two-sheet notice ("...Continued to the next page...",
#: "... Previous page Continuation..."). OCR keeps the line wherever it read
#: it — head, tail or mid-table — so a cue is looked for anywhere in the text.
CONTINUES_ON_NEXT = re.compile(
    r"contin(?:ued|ues)?\s*(?:to|on)\s*(?:the\s*)?next\s*page"
    r"|contd\.?\s*(?:to|on)\s*(?:the\s*)?next\s*page", re.I)
CONTINUES_FROM_PREVIOUS = re.compile(
    r"previous\s*page\s*continuation"
    r"|contin(?:ued|ues|uation)?\s*(?:of|from)\s*(?:the\s*)?previous\s*page"
    r"|contd\.?\s*from\s*(?:the\s*)?previous\s*page", re.I)


def page_cues(text: str) -> tuple[bool, bool]:
    """``(more follows this page, this page follows another)``, from its text."""
    t = text or ""
    return bool(CONTINUES_ON_NEXT.search(t)), bool(CONTINUES_FROM_PREVIOUS.search(t))


def order_pages_by_cues(pages: list[str], texts: dict[str, str]) -> dict:
    """Check a group's page order against the pages' own continuation lines.

    The order the detector hands over comes from ``downloads_list`` — the
    portal's file order, which is wrong on at least one live listing, where
    page 2 is attached first. Joining the pages in that order puts the tail of
    the lot table in front of the notice's own header, so the model reads the
    lots before it knows the bank, and every stored offset points into a text
    the notice never had.

    A page saying more follows it cannot be last; a page saying it continues
    another cannot be first. Returns ``{"pages", "changed", "conflict"}``:
    the order to use, whether it differs from the given one, and — when no
    order satisfies the cues — the reason to report instead of guessing.
    Silent (``changed`` false, ``conflict`` None) when the pages say nothing,
    which is most of them.
    """
    if len(pages) < 2:
        return {"pages": list(pages), "changed": False, "conflict": None}
    cues = {fn: page_cues(texts.get(fn, "")) for fn in pages}
    if not any(any(c) for c in cues.values()):
        return {"pages": list(pages), "changed": False, "conflict": None}

    def satisfied(order: list[str]) -> bool:
        return not cues[order[0]][1] and not cues[order[-1]][0]

    if satisfied(list(pages)):
        return {"pages": list(pages), "changed": False, "conflict": None}
    if len(pages) == 2:
        flipped = [pages[1], pages[0]]
        if satisfied(flipped):
            return {"pages": flipped, "changed": True, "conflict": None}
    return {"pages": list(pages), "changed": False,
            "conflict": f"{CUE_CONFLICT}: {', '.join(pages)}"}


def page_groups(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Group listing/document rows into ordered page groups.

    ``rows`` — one per (listing, document) edge:
        ``{"listing", "filename", "content_key", "position"}``
        where ``position`` is the file's index in that listing's
        ``downloads_list`` (None when absent).

    Returns ``(groups, ambiguous)``:

    * group     — ``{"pages": [filenames, page 1 first], "twins": [filenames]}``
                  ``twins`` are files whose text equals one of the pages and
                  that sit on exactly the leader's listings. A twin on any
                  other listing set is left out (making it a follower would
                  strand that listing's lots) and reported in ``ambiguous``
                  as ``{"filenames": [twin, leader], "reason": "twin outside
                  group: ..."}``.
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

    def flag(names: list[str], reason: str, keep_order: bool = False) -> None:
        key = frozenset(names)
        if key in flagged:
            return
        flagged.add(key)
        ambiguous.append({"filenames": list(names) if keep_order else sorted(names),
                          "reason": reason})

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
        twins: list[str] = []
        for twin in sorted(twins_for[key]):
            if doc_listings[twin] == base:
                twins.append(twin)
            else:
                flag([twin, leader],
                     f"{TWIN_OUTSIDE_GROUP}: {twin} is on {len(doc_listings[twin])} "
                     f"listings, {leader} is on {len(base)}", keep_order=True)
        groups.append({"pages": list(key), "twins": twins})
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
