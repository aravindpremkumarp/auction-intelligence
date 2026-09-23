"""Extract a long multi-lot notice a few lots at a time.

Why this exists
---------------
Asked for 40 lots in one read, the model returns a different subset each run
— 10, then 26, then something else — and a re-extraction simply replaces the
last result, so recall is a coin flip. Asked for five lots, it rarely drops
one. The notice text is the same either way, so reading it in small pieces
costs about the same and makes recall stable.

How a notice is cut
-------------------
A lot's block ends with its price line ("Reserve price: Rs.21,50,000/-"), so
every such line closes a lot. The notice is cut after every ``lots_per_chunk``
of them. The text after the last price line (auction dates per lot, terms,
contact) is notice-level and is appended to every chunk as context.

It is deliberately conservative: the cut is used only when the number of price
lines equals the reviewer-confirmed lot count. Anything else — a notice that
lists one reserve price for several properties, or repeats the price in a
summary table — returns ``None`` and the caller extracts the whole notice as
before. A wrong cut would split one lot across two chunks, which is worse than
a missed one.

Stitching the results
---------------------
Each chunk is extracted with ``expected_lot_count`` set to the lots it holds,
so the model numbers them 1..k. Those local indices are ranked by where each
lot first appears and shifted by the lots in earlier chunks, so every lot gets
a distinct global ``lot_index`` in reading order. Spans are mapped back to
offsets in the full notice. Entities read off the shared tail are kept once
(from the first chunk) so notice-level fields are not repeated per chunk.
"""
from __future__ import annotations

import re
from typing import Callable, NamedTuple

#: A price line that closes a lot: "Reserve price" followed closely by an
#: amount. The amount is required so prose like "not below the reserve price"
#: in the terms does not count as a lot.
_PRICE_LINE = re.compile(
    r"reserve\s*price[^\n]{0,40}?(?:rs\.?|₹|inr)\s*[\d]", re.I)

DEFAULT_LOTS_PER_CHUNK = 5


class Chunk(NamedTuple):
    start: int          # offset of the lot text in the full notice
    end: int
    lots: int           # price lines (= lots) in this chunk


class Plan(NamedTuple):
    chunks: list[Chunk]
    tail_start: int     # notice-level text after the last price line


def plan_chunks(markdown: str, expected_lot_count: int | None,
                lots_per_chunk: int = DEFAULT_LOTS_PER_CHUNK) -> Plan | None:
    """Where to cut ``markdown``, or ``None`` when it should be read whole."""
    if not markdown or not expected_lot_count or expected_lot_count <= lots_per_chunk:
        return None
    ends = []
    for m in _PRICE_LINE.finditer(markdown):
        nl = markdown.find("\n", m.end())
        ends.append(len(markdown) if nl < 0 else nl + 1)
    if len(ends) != int(expected_lot_count):
        return None
    chunks, start = [], 0
    for i in range(0, len(ends), lots_per_chunk):
        group = ends[i:i + lots_per_chunk]
        chunks.append(Chunk(start, group[-1], len(group)))
        start = group[-1]
    return Plan(chunks, ends[-1])


def _local_rank(ents: list[dict]) -> dict:
    """Map each local lot_index to its 1-based rank by first appearance."""
    first: dict = {}
    for e in ents:
        li = (e.get("attrs") or {}).get("lot_index")
        if li in (None, ""):
            continue
        pos = e.get("start") if e.get("start") is not None else 10 ** 9
        first[li] = min(first.get(li, pos), pos)
    return {li: r for r, li in enumerate(sorted(first, key=first.get), 1)}


def extract_chunked(markdown: str, plan: Plan,
                    read: Callable[[str, int], list[dict]]) -> list[dict]:
    """Run ``read(chunk_text, lots_in_chunk)`` per chunk and stitch the results.

    ``read`` returns stored-shape entity dicts (``_entities``) with offsets into
    the text it was given. The result has offsets into ``markdown`` and a
    global ``lot_index`` per lot.
    """
    tail = markdown[plan.tail_start:]
    out: list[dict] = []
    seen_tail: set = set()
    base = 0
    for n, c in enumerate(plan.chunks):
        body = markdown[c.start:c.end]
        text = body + ("\n\n" + tail if tail else "")
        tail_at = len(body) + (2 if tail else 0)
        ents = read(text, c.lots)
        rank = _local_rank([e for e in ents
                            if e.get("start") is None or e["start"] < len(body)])
        for e in ents:
            e = {**e, "attrs": dict(e.get("attrs") or {})}
            s, t = e.get("start"), e.get("end")
            in_tail = s is not None and s >= tail_at
            if in_tail:
                s, t = plan.tail_start + (s - tail_at), plan.tail_start + (t - tail_at)
                key = (e.get("cls"), s, t)
                if n > 0 or key in seen_tail:
                    continue
                seen_tail.add(key)
            elif s is not None:
                s, t = c.start + s, c.start + t
            e["start"], e["end"] = s, t
            li = e["attrs"].get("lot_index")
            if li not in (None, "") and li in rank and not in_tail:
                e["attrs"]["lot_index"] = str(base + rank[li])
            out.append(e)
        base += c.lots
    for i, e in enumerate(out):
        e["id"] = str(i)
    return out
