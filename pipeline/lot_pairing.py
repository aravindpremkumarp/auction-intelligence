"""Does each lot's description sit with its OWN reserve price and borrower?

The model links a notice's pieces into lots by stamping ``lot_index`` on
every entity, and ``apply_extractions.group_lots`` trusts that stamp. Nothing
checked it against WHERE the pieces are on the page, so a description read
into the wrong lot — lot 1's land under lot 2's reserve price — linked fine:
``match_lots_to_listings`` matches on reserve price, EMD and borrower first,
and only compares descriptions to break a tie. This module is that missing
position check. Pure code, no model call.

Two rules, each only where the page can actually decide it:

* **Crossed pairs.** A notice lists its lots in one order, whether each
  description comes before its price or after it, in one table or in
  paragraphs under a price table. So if lot A's description comes before lot
  B's, lot A's reserve price must come before lot B's too. A swap — A's
  description paired with B's price and B's with A's — breaks that order.

* **Split across lots.** When code can find the lots' own parts of the page
  without the model (``pipeline/reader/segment``: one table row per lot, or
  serial / numbered anchors running 1..n), a lot's reserve price and borrower
  must sit in the same part as its description. The ``price`` layout is not
  trusted here: it cuts at price lines, which presumes the description comes
  before its price — the very thing this check must not assume.

Only positions that can be trusted are judged (``_pinned``): the quote's
words must start where its span starts and appear nowhere else on the page.
Grounding snaps a repeated quote ("Rs.10,00,000" on two sibling flats) to
its first copy, and a long quote across table cells can land on a
neighbour's words — on 250 live multi-lot notices every "mix-up" the loose
version found was one of these, with the lot links themselves right. A
borrower named once for several lots is shared, not misplaced, and a cut
that does not give every lot exactly one price is not used at all.

What it cannot see: a uniform off-by-one (every description paired with the
NEXT lot's price) on a page with no lot numbers keeps both orders intact and
every pair adjacent. That needs the lot markers the split rule uses.
"""
from __future__ import annotations

import html
import re
from functools import lru_cache

#: Layouts whose lot boundaries come from the page itself, not from where the
#: prices are. See the module docstring for why ``price`` is excluded.
TRUSTED_LAYOUTS = frozenset({"table_row", "serial", "numbered"})


def _span(e) -> tuple[int, int] | None:
    ci = getattr(e, "char_interval", None)
    if ci is None:
        return None
    s, t = getattr(ci, "start_pos", None), getattr(ci, "end_pos", None)
    if s is None or t is None:
        return None
    return int(s), int(t)


def _has_reserve(e) -> bool:
    v = (e.attributes or {}).get("reserve_price_num")
    return v not in (None, "") and str(v).strip().lower() not in {"null", "na", "n/a"}


_TAG = re.compile(r"<[^>]+>")
_NOT_ALNUM = re.compile(r"[^a-z0-9]+")
#: How much of a quote must match for its place to count as found. Long
#: descriptions are compared on their opening words, which is where a
#: misplaced span shows (it lands on the wrong lot's opening words).
PIN_CHARS = 60
#: Shortest folded quote worth judging: "6.48" or "Rs.1" says nothing
#: about where it came from.
PIN_MIN = 6


def _fold(s: str) -> str:
    """Letters and digits only: what a quote and the page agree on once the
    page's HTML tags, entities, line breaks and punctuation are set aside."""
    return _NOT_ALNUM.sub("", html.unescape(_TAG.sub(" ", s or "")).lower())


@lru_cache(maxsize=8)
def _folded_page(source_text: str) -> str:
    return _fold(source_text)


def _pinned(start: int, text: str, source_text: str) -> bool:
    """True when the quote's words start at ``start`` and appear nowhere else
    on the page, so its position is the only place it could have come from.
    A fuzzy alignment (a span that lands on a neighbour's words, as a long
    quote across table cells can) and a repeated figure are left unjudged."""
    if not source_text:
        return True
    key = _fold(text)[:PIN_CHARS]
    if len(key) < PIN_MIN:
        return False
    here = _fold(source_text[start:start + 3 * len(key) + 200])
    return here.startswith(key) and _folded_page(source_text).count(key) == 1


def _collect(extractions, source_text: str) -> dict[str, dict]:
    """{lot: {described, desc, reserves: [(start, text)], borrowers: [...]}}.

    ``desc`` is where the lot's first description starts, and only when that
    quote is pinned: sibling flats often share word-for-word descriptions, and
    grounding puts every copy at the first one."""
    lots: dict[str, dict] = {}
    first_desc: dict[str, tuple[int, str]] = {}
    for e in extractions:
        sp = _span(e)
        if sp is None:
            continue
        a = e.attributes or {}
        li = str(a.get("lot_index") or "1")
        rec = lots.setdefault(li, {"described": False, "desc": None,
                                   "reserves": [], "borrowers": []})
        cls = e.extraction_class
        text = getattr(e, "extraction_text", "") or ""
        if cls == "full_description":
            rec["described"] = True
            if li not in first_desc or sp[0] < first_desc[li][0]:
                first_desc[li] = (sp[0], text)
        elif cls == "auction_terms" and _has_reserve(e):
            rec["reserves"].append((sp[0], text))
        elif cls == "borrower":
            rec["borrowers"].append((sp[0], text))
    for li, (start, text) in first_desc.items():
        if _pinned(start, text, source_text):
            lots[li]["desc"] = start
    return lots


def _crossed(lots: dict[str, dict], source_text: str) -> list[tuple[str, str]]:
    """Pairs of lots whose description order and reserve-price order disagree."""
    anchors: dict[str, tuple[int, int]] = {}
    for li, rec in lots.items():
        if rec["desc"] is None or not rec["reserves"]:
            continue
        r_start, r_text = min(rec["reserves"])
        if not _pinned(r_start, r_text, source_text):
            continue
        anchors[li] = (rec["desc"], r_start)
    out = []
    keys = sorted(anchors, key=lambda k: anchors[k])
    for i, a in enumerate(keys):
        da, ra = anchors[a]
        for b in keys[i + 1:]:
            db, rb = anchors[b]
            if da != db and ra != rb and (da - db) * (ra - rb) < 0:
                out.append((a, b))
    return out


def _part_of(segs, pos: int) -> int | None:
    for s in segs:
        if s.start <= pos < s.end:
            return s.index
    return None


def _split(lots: dict[str, dict], source_text: str) -> tuple[dict[str, list[str]], str]:
    """{lot: [what sits in another lot's part]} and the layout used."""
    if not source_text:
        return {}, ""
    try:
        from pipeline.reader.segment import segment
        seg = segment(source_text)
    except Exception:  # noqa: BLE001 - no trusted layout is a skip, not a failure
        return {}, ""
    if seg.strategy not in TRUSTED_LAYOUTS or len(seg.segments) < 2:
        return {}, seg.strategy
    segs = seg.segments
    # One lot states one reserve price, so a cut the extraction agrees with
    # holds exactly one pinned price in every part. Anything else — two
    # prices in one part, none in another — means the cut is wrong (an
    # "Item No 2" inside one lot read as a lot anchor), and a wrong cut
    # cannot judge the extraction, so nothing is judged.
    prices: dict[int, set[int]] = {}
    for rec in lots.values():
        for start, text in rec["reserves"]:
            p = _part_of(segs, start)
            if p is not None and _pinned(start, text, source_text):
                prices.setdefault(p, set()).add(start)
    if len(prices) != len(segs) or any(len(v) != 1 for v in prices.values()):
        return {}, seg.strategy
    homes = {li: _part_of(segs, rec["desc"]) for li, rec in lots.items()
             if rec["desc"] is not None}
    out: dict[str, list[str]] = {}
    for li, home in homes.items():
        if home is None:
            continue
        rec = lots[li]
        for label, key in (("reserve price", "reserves"), ("borrower", "borrowers")):
            for start, text in rec[key]:
                if not _pinned(start, text, source_text):
                    continue
                where = _part_of(segs, start)
                if where is None or where == home:
                    continue
                # One borrower often owns several lots and is written once:
                # the name is shared, not misplaced, when the lot whose part
                # it sits in claims it too.
                if any(h == where and (start, text) in lots[o][key]
                       for o, h in homes.items() if o != li):
                    continue
                if label not in out.setdefault(li, []):
                    out[li].append(label)
    return {k: v for k, v in out.items() if v}, seg.strategy


def lot_pairing(extractions, source_text: str = "") -> dict:
    """Position check of each lot's description against its reserve price
    and borrower. Returns {crossed: [(lot, lot)], split: {lot: [labels]},
    layout: str}. Empty on a notice with fewer than two described lots."""
    lots = _collect(extractions, source_text)
    if sum(1 for r in lots.values() if r["described"]) < 2:
        return {"crossed": [], "split": {}, "layout": ""}
    split, layout = _split(lots, source_text)
    return {"crossed": _crossed(lots, source_text), "split": split, "layout": layout}
