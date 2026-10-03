"""Char offset -> page and block, from ``Document.blocks``.

The markdown a notice is read from was assembled from OCR blocks
(pipeline/mineru.assemble_markdown: reading order, ``\\n\\n`` joins, page
numbers dropped). Walking the same blocks in the same order and finding each
block's text in the markdown gives every character a page and a block id;
an entity's span then names the page (and, through the block, the bbox) it
came from. That is the "show me where it says that" chain the August audit
called R3, computed once server-side rather than reconstructed in the UI.

Robust to drift: a block whose text is not found where expected is skipped,
and an offset outside every located block answers (None, None).
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass


@dataclass(frozen=True)
class Placed:
    start: int
    end: int
    page: int | None
    block_id: str
    label: str | None
    confidence: float | None


def _block_id(b: dict, i: int) -> str:
    for k in ("id", "block_id", "uid"):
        if b.get(k) not in (None, ""):
            return str(b[k])
    return f"p{b.get('page', 0)}b{b.get('reading_order', i)}"


def place_blocks(md: str, blocks: list[dict] | None) -> list[Placed]:
    """Each block located in ``md`` (in reading order), as absolute spans."""
    if not md or not blocks:
        return []
    skip = {"Discarded", "PageNumber"}
    ordered = sorted(
        ((i, b) for i, b in enumerate(blocks)
         if isinstance(b, dict) and b.get("label") not in skip),
        key=lambda ib: (int(ib[1].get("reading_order", 0) or 0), int(ib[1].get("page", 0) or 0)))
    out: list[Placed] = []
    cursor = 0
    for i, b in ordered:
        text = (b.get("text") or "").strip()
        if not text:
            continue
        probe = text[:80]
        j = md.find(probe, cursor)
        if j == -1:
            j = md.find(probe)
            if j == -1 or j < cursor:
                continue
        end = min(len(md), j + len(text))
        out.append(Placed(j, end, b.get("page"), _block_id(b, i), b.get("label"),
                          b.get("confidence")))
        cursor = end
    return out


class BlockMap:
    """Answers page/block for any offset, in O(log n)."""

    def __init__(self, md: str, blocks: list[dict] | None) -> None:
        self._md = md or ""
        self.placed = place_blocks(md, blocks)
        self._starts = [p.start for p in self.placed]

    def at(self, offset: int | None) -> Placed | None:
        if offset is None or not self.placed:
            return None
        k = bisect_right(self._starts, offset) - 1
        if k < 0:                      # a heading marker before the first block
            return self.placed[0] if offset < self.placed[0].end else None
        p = self.placed[k]
        # The joins between blocks ("\n\n", a "# ") belong to the block before.
        nxt = self.placed[k + 1].start if k + 1 < len(self.placed) else len(self._md)
        return p if p.start <= offset < max(p.end, nxt) else None

    def page_and_block(self, offset: int | None) -> tuple[int | None, str | None]:
        p = self.at(offset)
        return (p.page, p.block_id) if p else (None, None)


def page_and_block(md: str, blocks: list[dict] | None, offset: int | None
                   ) -> tuple[int | None, str | None]:
    """One-shot form of :class:`BlockMap` for callers with a single lookup."""
    return BlockMap(md, blocks).page_and_block(offset)


__all__ = ["Placed", "BlockMap", "place_blocks", "page_and_block"]
