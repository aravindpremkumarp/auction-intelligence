"""Number a notice's lots in the notice's own order — by review, not on save.

A whole-notice read lets the model number the lots, and now and then it
numbers them its own way — 6, 5, 4, 3, 2, 1 down a schedule, or the lot it
read first as lot 1. Lot 1 on the review page, on a listing and in the graph
should be lot 1 of the notice. Chunked reads (pipeline/lot_chunks) already
number by position; this does the same for any read.

``plan(ents, md, expected)`` decides the new numbers:

* **The notice's printed numbers**, when every lot has one just before its
  text ("SI.No. 4", a serial cell "| 4 |", "4.") and they run 1..n. A notice
  laid out in two columns prints 1, 9, 2, 10 … down the page, so its reading
  order is not its order — the printed numbers are.
* Otherwise, when the notice prints few or no numbers, **reading order**: lots
  sorted by where their description (or, lacking one, their first grounded
  fact) starts.

It refuses — returns no mapping and a reason — when it cannot place every lot,
when several lots share one description (a sub-schedule under one lot), or
when reading order says the lots run 4, 5, 6, 1, 2, 3: that is two pages
joined in the wrong order, and the fix is the page order, not the numbers.

Positions come from the read itself, and a messy read (a lot placed at a price
cell, half a description on the next lot) gives messy positions — so this
never runs on its own when a read is saved. scripts/renumber_lots reports what
it would change and writes a notice only when named.

``apply(ents, corrections, mapping)`` renumbers an extraction and everything
that names its lots by number: the reviewer's added entities and the
"not in the notice" / "not found" marks. A person's lot-match decision on a
listing names a lot by number too; callers skip notices that have one
(scripts/renumber_lots) rather than move it.
"""
from __future__ import annotations

import copy
import re

#: Entity classes that belong to one lot, in order of how well their start
#: marks where the lot begins.
_ANCHOR_CLASSES = ("full_description", "property", "borrower", "identifier",
                   "location", "extent", "auction_terms")

_MARK_RE = re.compile(r"^(absent|unfound):([^:]+):([a-z_]+)$")


def _lot(e: dict) -> str | None:
    li = (e.get("attrs") or {}).get("lot_index")
    return None if li in (None, "") else str(li)


def anchors(ents: list[dict]) -> dict[str, int]:
    """lot_index -> where the lot starts in the notice: its description's
    start, else its earliest grounded fact of the next class in
    ``_ANCHOR_CLASSES``. Lots with nothing grounded are left out."""
    by_lot: dict[str, list[dict]] = {}
    for e in ents:
        li = _lot(e)
        if li is not None and e.get("start") is not None:
            by_lot.setdefault(li, []).append(e)
    out: dict[str, int] = {}
    for li, mine in by_lot.items():
        for cls in _ANCHOR_CLASSES:
            starts = [e["start"] for e in mine if e.get("cls") == cls]
            if starts:
                out[li] = min(starts)
                break
    return out


def _is_rotation(seq: list[int]) -> bool:
    """4, 5, 6, 1, 2, 3: two increasing runs, the second starting at 1."""
    drops = [i for i in range(1, len(seq)) if seq[i] < seq[i - 1]]
    # Two runs of two lots or more; a swapped pair (2, 1) is just a swap.
    return (len(drops) == 1 and 2 <= drops[0] <= len(seq) - 2
            and seq[drops[0]] == min(seq)
            and seq[:drops[0]] == sorted(seq[:drops[0]])
            and seq[drops[0]:] == sorted(seq[drops[0]:]))


#: A lot's printed number just before its text: a table cell "| 9 |", a
#: "9." or "9)" paragraph number, or "Sl./SI./Lot/Item No. 9".
_LABEL = re.compile(
    r"(?:(?:sl|si|s|sr|lot|item|property)\.?\s*no\.?\s*[:\-]?\s*(\d{1,3})\b"
    r"|<td[^>]*>\s*(?:<[^>]+>\s*)*(\d{1,3})\s*\.?\s*(?:<[^>]+>\s*)*</td>"
    r"|(?:^|\n)[ \t]*(?:#+[ \t]*)?(?:\*\*)?[ \t]*(\d{1,3})[ \t]*[.)][ \t]+)",
    re.I)
#: How far before a lot's first fact its printed number may sit.
LABEL_REACH = 400


def _printed_numbers(md: str, at: dict[str, int]
                     ) -> tuple[dict[str, str] | None, int]:
    """``(numbers, found)``: lot -> the number the notice prints for it (the
    last lot number within ``LABEL_REACH`` characters before the lot's text,
    after the previous lot's text), and how many lots had one. ``numbers`` is
    None unless every lot has one and they run 1..n exactly once each."""
    starts = sorted(set(at.values()))
    out: dict[str, str] = {}
    for li, pos in at.items():
        prev = max((p for p in starts if p < pos), default=0)
        hits = list(_LABEL.finditer(md, max(prev, pos - LABEL_REACH), pos))
        if hits:
            out[li] = str(int(next(g for g in hits[-1].groups() if g)))
    complete = (len(out) == len(at)
                and sorted(int(v) for v in out.values()) == list(range(1, len(at) + 1)))
    return (out if complete else None), len(out)


def plan(ents: list[dict], md: str | None = None,
         expected_lot_count: int | None = None
         ) -> tuple[dict[str, str], str]:
    """``(mapping, reason)``. ``mapping`` is {old lot_index: new lot_index}
    for the lots that change — empty when nothing changes or it refuses, and
    ``reason`` says which.

    The notice's printed lot numbers decide when every lot has one: a notice
    in two columns prints 1, 9, 2, 10 … down the page, and its reading order
    is not its order. Reading order decides only when the notice prints no
    numbers. Lots that share one description (a sub-schedule) cannot be
    ordered by position at all, so those are refused.
    """
    lots = {li for e in ents if (li := _lot(e)) is not None}
    if len(lots) < 2:
        return {}, "single lot"
    at = anchors(ents)
    if set(at) != lots:
        missing = sorted(lots - set(at), key=lambda x: (len(x), x))
        return {}, f"cannot place lot(s) {missing}"
    if len(set(at.values())) != len(at):
        return {}, "several lots share one description — cannot order by position"
    order = sorted(at, key=lambda li: at[li])
    if not all(li.isdigit() for li in order):
        return {}, "non-numeric lot numbers"
    # Reading order only shuffles the numbers the read already has: a lot the
    # read missed leaves a gap (1, 3, 4, 5), and closing it would hide the miss
    # and give lot 3's facts lot 2's number.
    numbers = sorted(order, key=int)
    by_position = dict(zip(order, numbers))
    if all(old == new for old, new in by_position.items()):
        return {}, "already in order"
    new, found = _printed_numbers(md or "", at)
    how = "printed lot numbers"
    if new is None and found > len(at) // 2:
        # The notice numbers its lots, but not one number per extracted lot —
        # sub-items under one lot, a number printed twice. Its reading order
        # may not be its order (two columns print 1, 9, 2, 10 …), so neither
        # can be trusted: leave the numbers alone.
        return {}, (f"printed lot numbers found for {found} of {len(at)} lots, "
                    f"not 1..{len(at)} — left as is")
    if new is None:
        if _is_rotation([int(li) for li in order]):
            return {}, (f"lots run {', '.join(order)} in the text — pages joined "
                        f"in the wrong order? check the page order")
        new = by_position
        how = "reading order (no printed lot numbers)"
    mapping = {old: nw for old, nw in new.items() if old != nw}
    return mapping, (how if mapping else "already in order")


def apply(ents: list[dict], corrections: dict | None,
          mapping: dict[str, str]) -> tuple[list[dict], dict]:
    """``ents`` and ``corrections`` with lots renumbered by ``mapping``.
    Neither input is modified."""
    out = copy.deepcopy(ents)
    for e in out:
        li = _lot(e)
        if li in mapping:
            e["attrs"]["lot_index"] = mapping[li]
    corr = copy.deepcopy(corrections or {})
    moved: dict[str, dict] = {}
    for k in list(corr):
        v = corr[k]
        m = _MARK_RE.match(str(k))
        if m and m.group(2) in mapping:
            moved[f"{m.group(1)}:{mapping[m.group(2)]}:{m.group(3)}"] = v
            del corr[k]
        elif (isinstance(v, dict) and isinstance(v.get("attrs"), dict)
              and str(v["attrs"].get("lot_index")) in mapping):
            v["attrs"]["lot_index"] = mapping[str(v["attrs"]["lot_index"])]
    corr.update(moved)
    return out, corr

