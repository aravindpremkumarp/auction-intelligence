"""Extract a long multi-lot notice a few lots at a time.

Why this exists
---------------
Asked for 40 lots in one read, the model returns a different subset each run
— 10, then 26, then something else — and a re-extraction simply replaces the
last result, so recall is a coin flip. Asked for five lots, it rarely drops
one. The notice text is the same either way, so reading it in pieces costs
about the same and makes recall stable.

The flow
--------
::

    markdown ──► plan_chunks ──► Plan(lots=[Lot…], chunks=[Chunk…])   (code only)
                    │  no layout matches the confirmed lot count
                    └──► None ──► caller reads the notice whole, as before

    for each chunk:                                                    (model)
        text = head + table header + chunk's lots + tail
        read(text, k, hint "these are lots i–j of N")
        each lot-tagged entity ─► the lot whose span holds it (by position)
        lots with no evidence ─► retry: focused re-read → stronger model →
                                 re-read with the gap named   (≤ retries)

How a notice is cut
-------------------
Code, not the model, finds where each lot is. Three layouts cover the corpus,
tried in this order:

* ``price``    — every lot closes with its own price line ("Reserve price:
  Rs.21,50,000/-"), as the Canara prose lists do.
* ``serial``   — a table where each lot starts at a row whose first cell is
  its serial number, with continuation rows (EMD, possession) below it, as the
  Tata Capital schedules do.
* ``numbered`` — numbered paragraphs ("1. Name and Details of the Borrower",
  "No. 3 Name and address …").

A layout counts only if it yields EXACTLY the reviewer-confirmed lot count
(serial and numbered anchors must also run consecutively), and every lot it
yields mentions an amount of money. Anything else returns ``None``. A wrong
cut would split one lot across two chunks, which is worse than a missed one.

Numbering by position
---------------------
Because code knows where every lot is, the model never decides which lot an
entity belongs to when the entity is grounded: its span says. That is what
lets chunks be read independently — nothing restarts or collides across a
chunk boundary. The model's own ``lot_index`` is used only for entities it
could not ground and for per-lot values quoted from the shared tail, mapped
through the lots its grounded entities landed in.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Callable, NamedTuple

#: A price line that closes a lot: "Reserve price" followed closely by an
#: amount. The amount is required so prose like "not below the reserve price"
#: in the terms does not count as a lot.
_PRICE_LINE = re.compile(
    r"reserve\s*price[^\n]{0,40}?(?:rs\.?|₹|inr)\s*[\d]", re.I)

#: A table row whose first cell is a bare serial number ("<td>1</td>",
#: '<td rowspan="3">28</td>', '<td align="center">1.</td>').
_SERIAL_ROW = re.compile(
    r"<tr[^>]*>\s*<td[^>]*>\s*(?:<[^>]+>\s*)*(\d{1,3})\s*\.?\s*(?:<[^>]+>\s*)*</td>",
    re.I)

#: A paragraph that opens with a lot number: "No.1 Name…", "Sl. No. 4 …",
#: "Lot 2:", and the bare "3. Name and Details…".
_NUMBERED_PREFIXED = re.compile(
    r"(?im)^[ \t]*(?:#+[ \t]*)?(?:\*\*|<b>)?[ \t]*"
    r"(?:(?:sl|s|sr|lot|item|property)\.?[ \t]*no\.?|no\.?|lot)[ \t]*[:\-]?[ \t]*"
    r"(\d{1,3})\b")
_NUMBERED_BARE = re.compile(
    r"(?m)^[ \t]*(?:#+[ \t]*)?(?:\*\*|<b>)?[ \t]*(\d{1,3})[ \t]*[.)][ \t]+(?=[A-Za-z(])")

#: A lot that mentions no money is not a lot — it is a numbered clause.
_MONEY = re.compile(r"(?:rs\.?|₹|inr)\s*\.?\s*\d", re.I)

DEFAULT_LOTS_PER_CHUNK = 5
#: A chunk is also capped in characters, so five unusually long lots do not
#: rebuild the window-size problem this module exists to avoid.
CHUNK_CHAR_CAP = 16000
HEAD_CAP = 3000          # notice header repeated as context
TABLE_HEAD_CAP = 2500    # a table's header rows repeated as context
TAIL_CAP = 4000          # per-lot dates / terms after the last lot

#: Classes that are evidence a lot was actually read.
_LOT_EVIDENCE = frozenset({"borrower", "property", "full_description",
                           "auction_terms", "outstanding", "identifier",
                           "extent", "location"})


class Lot(NamedTuple):
    start: int
    end: int
    label: str | None   # the notice's own number for the lot, when it has one


class Chunk(NamedTuple):
    start: int          # offset of the chunk's lot text in the full notice
    end: int
    lots: int           # lots in this chunk
    first: int = 0      # index of its first lot in Plan.lots


class Plan(NamedTuple):
    chunks: list[Chunk]
    tail_start: int     # notice-level text after the last lot
    lots: list[Lot] = []
    strategy: str = "price"
    head_end: int = 0   # header before the first lot (0: lot 1 includes it)


# ── finding the lots ─────────────────────────────────────────────────────────

def _chain(anchors: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The longest run of anchors numbered n, n+1, n+2 … in reading order."""
    best: dict[int, list[tuple[int, int]]] = {}
    for pos, n in anchors:
        run = best.get(n - 1, []) + [(pos, n)]
        if len(run) > len(best.get(n, [])):
            best[n] = run
    return max(best.values(), key=len, default=[])


def _starts_to_lots(markdown: str,
                    anchors: list[tuple[int, int]]) -> tuple[list[Lot], int]:
    """Lots from their start positions. The last lot has no next start, so it
    runs as long as the longest earlier lot (at least 3000 chars), capped at the
    end of the text; that end is where the notice-level tail begins."""
    starts = [p for p, _ in anchors]
    lots = [Lot(s, e, str(n)) for (s, n), e in zip(anchors, starts[1:])]
    longest = max((lot.end - lot.start for lot in lots), default=0)
    last_end = min(len(markdown), starts[-1] + max(3000, int(longest * 1.5)))
    lots.append(Lot(starts[-1], last_end, str(anchors[-1][1])))
    return lots, last_end


def _price_end(markdown: str, at: int) -> int:
    """Where a lot closed by the price at ``at`` ends: the end of its table row
    when the price sits in one (HTML tables often put a whole schedule on ONE
    line, so the line end would swallow every later lot), else the line end."""
    nl = markdown.find("\n", at)
    nl = len(markdown) if nl < 0 else nl + 1
    row = markdown.find("</tr>", at, nl)
    return row + len("</tr>") if row >= 0 else nl


def _price_lots(markdown: str) -> tuple[list[Lot], int, int]:
    ends = []
    for m in _PRICE_LINE.finditer(markdown):
        end = _price_end(markdown, m.end())
        if not ends or end > ends[-1]:
            ends.append(end)
    lots, start = [], 0
    for e in ends:
        lots.append(Lot(start, e, None))
        start = e
    return lots, 0, (ends[-1] if ends else len(markdown))


def _cover(anchors: list[tuple[int, int]], n: int) -> list[tuple[int, int]]:
    """Anchors numbered exactly 1..n, one each, in reading order — for joined
    pages that came out of order (serials 13–24 before 1–12). Empty unless every
    number appears exactly once."""
    by_num: dict[int, list[int]] = {}
    for pos, k in anchors:
        by_num.setdefault(k, []).append(pos)
    if set(by_num) & set(range(1, n + 1)) != set(range(1, n + 1)):
        return []
    if n + 1 in by_num:          # the run goes on past n: the count is wrong
        return []
    if any(len(by_num[k]) != 1 for k in range(1, n + 1)):
        return []
    return sorted((by_num[k][0], k) for k in range(1, n + 1))


def _anchored_lots(markdown: str, anchors: list[tuple[int, int]],
                   n: int | None = None) -> tuple[list[Lot], int, int]:
    run = _chain(sorted(anchors))
    if n and len(run) != n:
        run = _cover(anchors, n) or run
    if not run:
        return [], 0, len(markdown)
    lots, tail = _starts_to_lots(markdown, run)
    return lots, run[0][0], tail


def _layouts(markdown: str, n: int | None = None):
    yield "price", _price_lots(markdown)
    yield "serial", _anchored_lots(
        markdown, [(m.start(), int(m.group(1)))
                   for m in _SERIAL_ROW.finditer(markdown)], n)
    numbered = [(m.start(), int(m.group(1)))
                for rx in (_NUMBERED_PREFIXED, _NUMBERED_BARE)
                for m in rx.finditer(markdown)]
    yield "numbered", _anchored_lots(markdown, numbered, n)


def find_lots(markdown: str, expected_lot_count: int | None
              ) -> tuple[str, list[Lot], int, int] | None:
    """``(strategy, lots, head_end, tail_start)`` for the first layout that
    yields exactly ``expected_lot_count`` money-bearing lots, else ``None``."""
    if not markdown or not expected_lot_count:
        return None
    n = int(expected_lot_count)
    for name, (lots, head_end, tail_start) in _layouts(markdown, n):
        if len(lots) == n and all(_MONEY.search(markdown, lot.start, lot.end)
                                  for lot in lots):
            return name, lots, head_end, tail_start
    return None


def plan_chunks(markdown: str, expected_lot_count: int | None,
                lots_per_chunk: int = DEFAULT_LOTS_PER_CHUNK) -> Plan | None:
    """Where to cut ``markdown``, or ``None`` when it should be read whole."""
    if not expected_lot_count or int(expected_lot_count) <= lots_per_chunk:
        return None
    found = find_lots(markdown, expected_lot_count)
    if found is None:
        return None
    strategy, lots, head_end, tail_start = found
    chunks: list[Chunk] = []
    i = 0
    while i < len(lots):
        j = i + 1
        while (j < len(lots) and j - i < lots_per_chunk
               and lots[j].end - lots[i].start <= CHUNK_CHAR_CAP):
            j += 1
        chunks.append(Chunk(lots[i].start, lots[j - 1].end, j - i, i))
        i = j
    return Plan(chunks, tail_start, lots, strategy, head_end)


# ── composing a chunk's text ─────────────────────────────────────────────────

class _Text(NamedTuple):
    text: str
    segments: list[tuple[int, int, int]]   # (offset in text, offset in notice, length)
    body: list[tuple[int, int]]            # notice ranges that are lot text
    tail_at: int                           # notice offset where the tail starts


def _table_head(markdown: str, at: int, lots: list[Lot]) -> tuple[int, int] | None:
    """The header rows of the table ``at`` sits inside, or None if it is not
    inside a table. They end where the table's first lot starts."""
    opened = markdown.rfind("<table", 0, at)
    if opened < 0 or markdown.rfind("</table>", 0, at) > opened:
        return None
    first = min((lot.start for lot in lots if lot.start >= opened), default=at)
    end = min(first, at, opened + TABLE_HEAD_CAP)
    return (opened, end) if end > opened else None


def _compose(markdown: str, plan: Plan, lot_ids: list[int]) -> _Text:
    """Header, table header, the given lots and the tail, with a map back."""
    ranges: list[tuple[int, int]] = []
    if plan.head_end:
        ranges.append((0, min(plan.head_end, HEAD_CAP)))
    body = [(plan.lots[i].start, plan.lots[i].end) for i in lot_ids]
    th = _table_head(markdown, body[0][0], plan.lots)
    if th and (not ranges or th[0] >= ranges[-1][1]):
        ranges.append(th)
    ranges.extend(body)
    if plan.tail_start < len(markdown):
        ranges.append((plan.tail_start,
                       min(len(markdown), plan.tail_start + TAIL_CAP)))
    parts, segments, off = [], [], 0
    for s, e in ranges:
        if parts:
            parts.append("\n\n")
            off += 2
        parts.append(markdown[s:e])
        segments.append((off, s, e - s))
        off += e - s
    return _Text("".join(parts), segments, body, plan.tail_start)


def _to_notice(t: _Text, start: int | None, end: int | None):
    if start is None:
        return None, None
    for off, orig, length in t.segments:
        if off <= start < off + length:
            s = orig + (start - off)
            e = orig + min((end if end is not None else start) - off, length)
            return s, e
    return None, None


# ── stitching ────────────────────────────────────────────────────────────────

def _lot_at(plan: Plan, pos: int | None, lot_ids: list[int]) -> int | None:
    if pos is None:
        return None
    for i in lot_ids:
        if plan.lots[i].start <= pos < plan.lots[i].end:
            return i
    return None


def _place(markdown: str, plan: Plan, t: _Text, lot_ids: list[int],
           ents: list[dict]) -> list[tuple[dict, int | None]]:
    """Each entity with notice offsets and the lot it belongs to (None for
    notice-level). Grounded lot-tagged entities are placed by position; the
    rest follow what the model's own lot_index meant, learned from where its
    grounded entities landed."""
    placed, votes = [], {}
    for e in ents:
        e = {**e, "attrs": dict(e.get("attrs") or {})}
        e["start"], e["end"] = _to_notice(t, e.get("start"), e.get("end"))
        li = e["attrs"].get("lot_index")
        lot = _lot_at(plan, e["start"], lot_ids) if li not in (None, "") else None
        if lot is not None:
            votes.setdefault(str(li), Counter())[lot] += 1
        placed.append([e, lot])
    meaning = {li: c.most_common(1)[0][0] for li, c in votes.items()}
    for row in placed:
        e, lot = row
        li = e["attrs"].get("lot_index")
        if lot is None and li not in (None, ""):
            row[1] = meaning.get(str(li))
            if row[1] is None:
                # A lot tag we cannot place: keep the entity, but not a guess.
                e["attrs"].pop("lot_index", None)
    return [(e, lot) for e, lot in placed]


def _read_lots(placed: list[tuple[dict, int | None]]) -> set[int]:
    return {lot for e, lot in placed
            if lot is not None and e.get("cls") in _LOT_EVIDENCE}


_FOCUS_HINT = ("This excerpt holds {k} lot(s) that an earlier read missed. "
               "Each one is a separate lot: extract every one of them, with "
               "all of its entities.")
_GAP_HINT = ("This excerpt holds {k} lots; an earlier read found only {got}. "
             "Go through it lot by lot, from the first to the last, and extract "
             "every lot — including the ones starting: {starts}.")


def _position_hint(plan: Plan, lot_ids: list[int]) -> str:
    """Tell the read where it is: the code knows, so the model need not."""
    n = len(plan.lots)
    a, b = lot_ids[0] + 1, lot_ids[-1] + 1
    labels = [plan.lots[i].label for i in lot_ids if plan.lots[i].label]
    named = (f" (numbered {labels[0]}–{labels[-1]} in the notice)"
             if len(labels) == len(lot_ids) and len(labels) > 1 else "")
    return (f"This is an excerpt of a notice selling {n} lots. It holds lots "
            f"{a}–{b}{named}; the notice's header and closing text are "
            f"included only as context. Number the lots in this excerpt 1 to "
            f"{len(lot_ids)} in the order they appear.")


def extract_chunked(markdown: str, plan: Plan,
                    read: Callable[..., list[dict]],
                    retries: int = 3) -> list[dict]:
    """Read each chunk, place every entity on its lot, and retry the gaps.

    ``read(text, lots, extra=None, strong=False)`` returns stored-shape entity
    dicts (``_entities``) with offsets into ``text``. ``extra`` is appended to
    the prompt; ``strong`` asks for the stronger model. The result has offsets
    into ``markdown`` and a global ``lot_index`` in reading order.

    A lot with no evidence after its chunk's read is retried, at most
    ``retries`` times, each time differently: its missing lots alone, then the
    same with the stronger model, then the whole chunk with the gap named.
    """
    out: list[dict] = []
    seen_notice: set = set()

    def keep(placed, lots_wanted: set[int] | None):
        for e, lot in placed:
            if lot is None:
                if e["attrs"].get("lot_index") not in (None, ""):
                    continue
                key = (e.get("cls"), e.get("start"), e.get("end"), e.get("text"))
                if lots_wanted is not None or key in seen_notice:
                    continue
                seen_notice.add(key)
            elif lots_wanted is not None and lot not in lots_wanted:
                continue
            else:
                e["attrs"]["lot_index"] = str(lot + 1)
            out.append(e)

    for c in plan.chunks:
        ids = list(range(c.first, c.first + c.lots))
        t = _compose(markdown, plan, ids)
        placed = _place(markdown, plan, t, ids,
                        read(t.text, c.lots, extra=_position_hint(plan, ids)))
        keep(placed, None)
        missing = [i for i in ids if i not in _read_lots(placed)]
        for attempt in range(retries):
            if not missing:
                break
            try:
                if attempt < 2:
                    ft = _compose(markdown, plan, missing)
                    got = _place(markdown, plan, ft, missing,
                                 read(ft.text, len(missing),
                                      extra=_FOCUS_HINT.format(k=len(missing)),
                                      strong=attempt == 1))
                else:
                    starts = "; ".join(
                        repr(" ".join(markdown[plan.lots[i].start:
                                               plan.lots[i].start + 80].split()))
                        for i in missing)
                    got = _place(markdown, plan, t, ids,
                                 read(t.text, c.lots, extra=_GAP_HINT.format(
                                     k=c.lots, got=c.lots - len(missing),
                                     starts=starts), strong=True))
            except Exception:  # a failed retry must not cost the lots already read
                continue
            found = _read_lots(got) & set(missing)
            keep(got, found)
            missing = [i for i in missing if i not in found]
    for i, e in enumerate(out):
        e["id"] = str(i)
    return out


def lots_read(ents: list[dict]) -> int:
    """Distinct lots with evidence in stored entities."""
    return len({str(e["attrs"]["lot_index"]) for e in ents
                if e.get("cls") in _LOT_EVIDENCE
                and (e.get("attrs") or {}).get("lot_index") not in (None, "")})
