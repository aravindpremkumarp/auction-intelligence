"""Cut a notice into lots by code — no reviewer count required.

pipeline/lot_chunks finds lots with three layouts (price lines, serial rows,
numbered paragraphs) but only trusts a layout that yields EXACTLY the
reviewer-confirmed count, so an unreviewed notice is always read whole. This
module keeps those layouts (imported, not copied), adds table rows as the
first choice, and states when a layout can be trusted WITHOUT a count:

* ``table_row``  — one ``<tr>`` per lot under a header that names the price
                   column, every row carrying a figure there (tables.lot_rows);
* ``serial`` / ``numbered`` — anchors that run 1, 2, 3 … consecutively from
                   1, at least two of them, every lot mentioning money;
* ``price``      — only when the notice has no serial or numbered anchors at
                   all (a price cut of a numbered notice hands each lot's
                   closing lines to the next lot).

With a count, today's exact-match rule applies (find_lots). Two anchored
layouts that disagree, or none that fits on a notice that plainly has
several lots (≥ 3 lot markers, or > 20k characters), go to the model — but
only for the lots' FIRST and LAST WORDS (schema.LotBoundaries), which code
then locates, orders and checks for money. Any doubt falls back to
``whole``: one segment, the whole notice, read with the ``lots[]`` schema.
A wrong cut splits a lot across two reads, which is worse than a missed lot.

``chunks`` groups segments a few at a time and composes each chunk's text
with the notice head, the table header rows and the tail
(lot_chunks._compose), so a column unit or a shared auction date is in
every excerpt — the failure that sank the per-lot ContextGem prototype.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from pipeline.lot_chunks import (
    CHUNK_CHAR_CAP, DEFAULT_LOTS_PER_CHUNK, _MONEY, _NUMBERED_BARE,
    _NUMBERED_PREFIXED, _SERIAL_ROW, Chunk, Lot, Plan, _Text, _compose,
    _layouts, _one_price_each, _price_spills, _to_notice, find_lots,
)
from pipeline.reader.ground import Window, locate
from pipeline.reader.tables import Table, lot_rows, parse_tables

#: A notice this long, or with this many "S.No / Sl.No / Item No" markers,
#: plainly holds several lots; reading it whole is the last resort.
LONG_NOTICE_CHARS = 20_000
LOT_MARKER_MIN = 3
_LOT_MARKER = re.compile(r"\b(S\.?\s?No|Sr\.?\s?No|Sl\.?\s?No|Item\s*No|Lot\s*No)\b", re.I)
#: A reserve (or upset) price stated with a figure, with or without "Rs." —
#: broader than lot_chunks._PRICE_LINE, which needs the currency mark and so
#: missed "RESERVE PRICE 35.15,000/-". One lot states its reserve once, so
#: without a reviewer count the number of these must equal the number of
#: lots a layout found: six reserves under three serials means a serial is
#: hiding two lots (750348), five under two means the serials restart per
#: branch (753006). Either way the cut is wrong and the notice is read whole.
_RESERVE_FIGURE = re.compile(r"(?:reserve|upset)\s*price[^\n<]{0,40}?\d", re.I)


@dataclass(frozen=True)
class Segment:
    start: int
    end: int
    index: int                        # 0-based position; lot_index = index + 1
    label: str | None = None          # the notice's own number, when it has one
    row: tuple[int, int] | None = None  # (table index, row index) for table_row


@dataclass
class Segmentation:
    strategy: str                     # table_row | serial | numbered | price | anchors | whole
    segments: list[Segment]
    head_end: int = 0                 # notice head before the first lot
    tail_start: int = 0               # notice-level text after the last lot
    tables: list[Table] = field(default_factory=list)
    expected: int | None = None
    note: str = ""                    # why this strategy (for telemetry)

    @property
    def whole(self) -> bool:
        return self.strategy == "whole"

    def plan(self) -> Plan:
        lots = [Lot(s.start, s.end, s.label) for s in self.segments]
        return Plan([], self.tail_start, lots, self.strategy, self.head_end)

    def at(self, pos: int | None) -> Segment | None:
        if pos is None:
            return None
        return next((s for s in self.segments if s.start <= pos < s.end), None)

    def window(self, seg: Segment, md: str) -> Window:
        return Window(md, seg.start, seg.end)


@dataclass
class ChunkText:
    segment_ids: list[int]
    text: str
    back: _Text
    strategy: str

    def to_notice(self, start: int | None, end: int | None) -> tuple[int | None, int | None]:
        return _to_notice(self.back, start, end)


# ── layouts ───────────────────────────────────────────────────────────────────

def _money_in(md: str, lots: list[Lot]) -> bool:
    return bool(lots) and all(_MONEY.search(md, lot.start, lot.end) for lot in lots)


def _consecutive_from_one(lots: list[Lot]) -> bool:
    try:
        labels = [int(lot.label) for lot in lots]
    except (TypeError, ValueError):
        return False
    return labels == list(range(1, len(labels) + 1))


def _reserve_count_agrees(md: str, n: int) -> bool:
    """True when the notice states as many reserve prices as ``n`` lots, or
    states none in a labelled form (a table keeps the label in its header)."""
    k = len(_RESERVE_FIGURE.findall(md))
    return k == 0 or k == n


def _has_anchors(md: str) -> bool:
    return bool(_SERIAL_ROW.search(md) or _NUMBERED_PREFIXED.search(md)
                or _NUMBERED_BARE.search(md))


#: A row is a lot's WHOLE segment only when the lot's description is in the
#: row. Tata-style notices put each lot's description in a paragraph after
#: its table; there the row holds the money and the serial layout (row start
#: to next row start) is the right cut, so these rows are left to it.
_DESCRIPTION_CLUE = re.compile(
    r"piece\s+and\s+parcel|survey|\bs\.?\s*(?:f\.?\s*)?no\b|door\s*no|plot\s*no|flat\s*no|"
    r"situated|bounded|boundar|\bsq\.?\s*f|acre|cents?\b|village|taluk", re.I)


def _table_segments(md: str, tables: list[Table]) -> tuple[list[Segment], int, int] | None:
    segs: list[Segment] = []
    for ti, t in enumerate(tables):
        rows = lot_rows(t)
        if not rows:
            continue
        if not all(_DESCRIPTION_CLUE.search(md, r.start, r.end) for r in rows):
            return None
        for r in rows:
            segs.append(Segment(r.start, r.end, len(segs), None, (ti, r.index)))
    if len(segs) < 1:
        return None
    first_table = min(t.start for t in tables if lot_rows(t))
    last_table = max(t.end for t in tables if lot_rows(t))
    return segs, first_table, last_table


#: A paragraph that still belongs to the lot whose price line came just
#: before it: the terms a notice prints after the reserve price (bid
#: increment, EMD, the property id, the auction website, an image link) and
#: a bare id token. Gold 753006 prints "Bid increment", "PROPERTY ID NO." and
#: "EMD" after every reserve price; cutting at the price line handed each
#: lot's id and EMD to the next lot's window, where grounding refused them.
_TRAILING_TERMS = re.compile(
    r"^\s*(?:\*\*)?(?:bid\s*increment|property\s*id|emd\b|earnest|e-?auction\s*(?:web\s*site|portal)"
    r"|property\s*location|website|!\[|https?://|www\.|[A-Z]{2,}\d{6,}|cersai)", re.I)
TRAILING_TERMS_CAP = 1200


def _extend_price_tails(md: str, lots: list[Lot], tail_start: int) -> tuple[list[Lot], int]:
    """Move each price-cut boundary past the terms paragraphs that follow
    the price line. The next lot starts where the previous now ends."""
    out: list[Lot] = []
    for i, lot in enumerate(lots):
        end = lot.end
        limit = min(len(md), lot.end + TRAILING_TERMS_CAP)
        if i + 1 < len(lots):
            limit = min(limit, lots[i + 1].end - 1)
        pos = end
        while pos < limit:
            while pos < limit and md[pos] in "\n \t":
                pos += 1
            nxt = md.find("\n\n", pos)
            nxt = limit if nxt == -1 or nxt > limit else nxt
            para = md[pos:nxt]
            if not para or not _TRAILING_TERMS.match(para):
                break
            end = nxt
            pos = nxt
        start = out[-1].end if out else lot.start
        out.append(Lot(start, max(end, start), lot.label))
    last_end = out[-1].end if out else tail_start
    return out, max(tail_start, last_end)


def _from_lots(strategy: str, lots: list[Lot], head_end: int, tail_start: int,
               md: str, expected: int | None, note: str = "") -> Segmentation:
    if strategy == "price":
        lots, tail_start = _extend_price_tails(md, lots, tail_start)
    segs = [Segment(l.start, l.end, i, l.label) for i, l in enumerate(lots)]
    return Segmentation(strategy, segs, head_end, tail_start, parse_tables(md), expected, note)


def _whole(md: str, expected: int | None, note: str, tables: list[Table]) -> Segmentation:
    return Segmentation("whole", [Segment(0, len(md), 0)], 0, len(md), tables, expected, note)


def _plainly_multi(md: str, expected: int | None) -> bool:
    return ((expected or 0) > 1 or len(md) > LONG_NOTICE_CHARS
            or len(_LOT_MARKER.findall(md)) >= LOT_MARKER_MIN)


def _anchor_segments(md: str, boundaries, expected: int | None
                     ) -> list[Segment] | None:
    """Locate the model's first/last words in order; None on any doubt."""
    lots = getattr(boundaries, "lots", None) or []
    if len(lots) < 2 or (expected and len(lots) != expected):
        return None
    w = Window(md)
    segs: list[Segment] = []
    cursor = 0
    for i, b in enumerate(lots):
        s = locate(w, b.first_words, cursor)
        if s is None or s.start < cursor:
            return None
        e = locate(Window(md, s.start), b.last_words, s.start)
        if e is None or e.end <= s.start:
            return None
        if not _MONEY.search(md, s.start, e.end):
            return None
        segs.append(Segment(s.start, e.end, i, b.label))
        cursor = e.end
    return segs


def segment(md: str, expected_lot_count: int | None = None,
            boundary_reader: Callable[[str], object] | None = None,
            tables: list[Table] | None = None) -> Segmentation:
    """Where each lot of ``md`` is. Never raises; ``whole`` when unsure."""
    md = md or ""
    expected = int(expected_lot_count) if expected_lot_count else None
    tables = tables if tables is not None else parse_tables(md)
    if not md.strip():
        return _whole(md, expected, "empty", tables)

    # A one-lot notice is never cut: its lot is the whole notice. Cutting it
    # to its table row (gold 752245) put the property description that
    # follows the table outside the lot's window, and every quote from it
    # was dropped as not on the page.
    if expected == 1:
        return _whole(md, expected, "single", tables)

    # 1. table rows
    ts = _table_segments(md, tables)
    if ts:
        segs, head_end, tail_start = ts
        if len(segs) >= 2 and ((expected is None and _reserve_count_agrees(md, len(segs)))
                               or expected == len(segs)):
            return Segmentation("table_row", segs, head_end, tail_start, tables, expected,
                                f"{len(segs)} rows under a price column")

    # 2. with a count: the exact-match rule the chunker already trusts
    if expected:
        found = find_lots(md, expected)
        if found is not None:
            strategy, lots, head_end, tail_start = found
            if expected == 1:
                return _whole(md, expected, "single", tables)
            return _from_lots(strategy, lots, head_end, tail_start, md, expected,
                              f"exact match to reviewer count {expected}")
    else:
        # 3. without a count: anchored layouts that run 1..n with money each
        fits = {}
        for name, (lots, head_end, tail_start) in _layouts(md, None):
            if name == "price":
                continue
            if (len(lots) >= 2 and _money_in(md, lots) and _consecutive_from_one(lots)
                    and _reserve_count_agrees(md, len(lots))):
                fits[name] = (lots, head_end, tail_start)
        if fits:
            counts = {len(v[0]) for v in fits.values()}
            if len(counts) == 1:
                name = "serial" if "serial" in fits else "numbered"
                lots, head_end, tail_start = fits[name]
                return _from_lots(name, lots, head_end, tail_start, md, None,
                                  f"{len(lots)} {name} anchors 1..{len(lots)}")
            # serial and numbered disagree: doubt -> anchors / whole below
        elif not _has_anchors(md):
            lots, head_end, tail_start = next(v for k, v in _layouts(md, None) if k == "price")
            if len(lots) >= 2 and _money_in(md, lots) and _reserve_count_agrees(md, len(lots)):
                return _from_lots("price", lots, head_end, tail_start, md, None,
                                  f"{len(lots)} price lines, no anchors")

    # 4. the model names boundaries; code checks them
    if boundary_reader is not None and _plainly_multi(md, expected):
        try:
            segs = _anchor_segments(md, boundary_reader(md), expected)
        except Exception:  # noqa: BLE001 - a failed call is a doubt, not a crash
            segs = None
        if segs:
            return Segmentation("anchors", segs, segs[0].start, segs[-1].end, tables,
                                expected, f"{len(segs)} model anchors verified")

    return _whole(md, expected, "no trusted layout", tables)


# ── chunks ────────────────────────────────────────────────────────────────────

def chunks(seg: Segmentation, md: str, lots_per_chunk: int = DEFAULT_LOTS_PER_CHUNK,
           char_cap: int = CHUNK_CHAR_CAP) -> list[ChunkText]:
    """The texts to read: a few lots each, with head / table header / tail
    as context and a map back to notice offsets. A ``whole`` segmentation
    is one chunk of the whole notice."""
    if seg.whole or not seg.segments:
        return [ChunkText([0], md, _Text(md, [(0, 0, len(md))], [(0, len(md))], len(md)),
                          "whole")]
    plan = seg.plan()
    out: list[ChunkText] = []
    i = 0
    n = len(seg.segments)
    while i < n:
        j = i + 1
        while (j < n and j - i < lots_per_chunk
               and seg.segments[j].end - seg.segments[i].start <= char_cap):
            j += 1
        ids = list(range(i, j))
        out.append(ChunkText(ids, *_compose_chunk(md, plan, ids), seg.strategy))
        i = j
    return out


def _compose_chunk(md: str, plan: Plan, ids: list[int]) -> tuple[str, _Text]:
    t = _compose(md, plan, ids)
    return t.text, t


def halve(chunk: ChunkText, seg: Segmentation, md: str) -> list[ChunkText]:
    """Two chunks from one, for a read that hit max_tokens. A single-segment
    chunk cannot be split further and comes back alone."""
    if len(chunk.segment_ids) < 2:
        return [chunk]
    mid = len(chunk.segment_ids) // 2
    plan = seg.plan()
    return [ChunkText(ids, *_compose_chunk(md, plan, ids), seg.strategy)
            for ids in (chunk.segment_ids[:mid], chunk.segment_ids[mid:])]


def describe(seg: Segmentation) -> dict:
    """For telemetry / the extraction_segmentation stamp."""
    return {"strategy": seg.strategy, "lots": len(seg.segments), "note": seg.note,
            "expected": seg.expected, "tables": len(seg.tables)}


__all__ = ["Segment", "Segmentation", "ChunkText", "segment", "chunks", "halve",
           "describe", "LONG_NOTICE_CHARS", "LOT_MARKER_MIN", "Chunk"]


if __name__ == "__main__":  # python -m pipeline.reader.segment file.txt [expected]
    import sys
    text = open(sys.argv[1], encoding="utf-8").read()
    exp = int(sys.argv[2]) if len(sys.argv) > 2 else None
    s = segment(text, exp)
    print(f"{s.strategy}: {len(s.segments)} segment(s) — {s.note}")
    for sg in s.segments:
        print(f"  [{sg.index + 1}] {sg.start}-{sg.end} label={sg.label!r} row={sg.row}: "
              f"{text[sg.start:sg.start + 70]!r}")
    for c in chunks(s, text):
        print(f"chunk {c.segment_ids}: {len(c.text)} chars")
