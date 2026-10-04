"""Widen a lot's full_description over the details it stopped short of.

full_description is meant to be the verbatim union of a lot's descriptive
detail (pipeline/validators.full_description_coverage). The model often ends
the block one line early: at "Bounded by:" with the boundaries after it, after
Schedule A with Schedule B / C below, before the UDS sentence. Those details
are extracted — each with its own span — so the text the block should have
covered is known, and stretching the block over it needs no model call.

``widen`` does that for every lot whose block misses a detail the validator
counts as a real truncation (not one it excuses: a portal ID, the borrower's
address, a repeat in a table — see ``_outside_reason``). It stretches the
block's edge outwards, detail by detail, nearest first, and stops at the first
one it may not reach:

* farther than ``MAX_GAP`` characters from the edge — a schedule elsewhere in
  the notice, not the next line of this one;
* past anything that is not this lot's description: another lot's block or
  any of its entities, or a price, borrower, bank, contact or terms entity of
  any lot; a rupee amount or a "reserve price" / "EMD" label; the heading of
  the next property ("Item No-02", "Property No.2"). A description that
  swallowed the reserve price or the next property would be worse than one a
  line short.

The block's new text is always ``markdown[start:end]``, and the entity keeps
``widened_from`` (its old span), so the change is visible and reversible.
A detail it could not reach stays outside, still flagged.

The same pass is the deterministic counterpart of reader v2's
``convert.block_span``, which widens its own blocks over must-cover spans.
"""
from __future__ import annotations

import re

from pipeline.extraction_ids import assign_ids
from pipeline.validators import (
    _DESCRIPTION_CLASSES, _alnum, _covered_by_text, _norm_ws, _outside_reason,
    normalize_identifier_kind,
)

#: Farthest a block edge moves to reach one detail (characters of notice text
#: between the edge and the detail). A boundary list or a Schedule B directly
#: below the block is well inside it; a detail farther away is elsewhere.
MAX_GAP = 600
#: How far past the last reached detail the edge may run to finish its line.
LINE_TAIL = 200
#: Classes that may sit inside a lot's description.
_INSIDE_OK = _DESCRIPTION_CLASSES | {"full_description", "extras"}
#: The heading of the next property: "Item No-02", "Property No.2 :",
#: "Lot 3", "Item No. II". The block never runs past one — the details beyond
#: it are another property's, however the model tagged them (and a notice read
#: as one lot that holds several items is a lot count to fix, not to hide).
_NEXT_PROPERTY = re.compile(
    r"(?:^|\n|\||<t[dh][^>]*>|#)\W{0,6}(?:item|property|lot)\s*(?:no\.?|number)?"
    r"\s*[:.\-]?\s*(?:\d|[ivx]+\b)", re.I)
#: Money and auction terms: a description never runs into the price column
#: or the terms, entity or not.
_TERMS = re.compile(r"(?:rs\.?|₹|inr)\s*[\d,]{4,}|reserve\s*price|earnest\s*money|\bemd\b",
                    re.I)


def _lot(e: dict) -> str:
    return str((e.get("attrs") or {}).get("lot_index") or "1")


def _span(e: dict) -> tuple[int, int] | None:
    s, t = e.get("start"), e.get("end")
    return (s, t) if isinstance(s, int) and isinstance(t, int) and t >= s else None


def _blocked(lo: int, hi: int, lot: str, ents: list[dict], own: set[int],
             md: str = "") -> bool:
    """Is there anything in (lo, hi) the lot's description may not swallow?"""
    if md and (_NEXT_PROPERTY.search(md, lo, hi) or _TERMS.search(md, lo, hi)):
        return True
    for i, e in enumerate(ents):
        if i in own:
            continue
        sp = _span(e)
        if not sp or sp[1] <= lo or sp[0] >= hi:
            continue
        if _lot(e) != lot or e.get("cls") not in _INSIDE_OK:
            return True
    return False


def _line_end(md: str, at: int, limit: int) -> int:
    nl = md.find("\n", at, min(limit, at + LINE_TAIL))
    return nl if nl != -1 else at


def _line_start(md: str, at: int, limit: int) -> int:
    nl = md.rfind("\n", max(limit, at - LINE_TAIL), at)
    return nl + 1 if nl != -1 else at


def _missed(ents: list[dict], md: str, lot: str, fd_idx: list[int],
            other_blocks: list) -> list[tuple[int, int]]:
    """Spans of this lot's details the validator counts as real truncations."""
    spans = [_span(ents[i]) for i in fd_idx]
    fd = {"span": (min(s for s, _ in spans), max(t for _, t in spans)),
          "text": " ".join(_norm_ws(ents[i].get("text")) for i in fd_idx).strip()}
    fd["alnum"] = _alnum(fd["text"])
    blocks: dict = {}
    for ol, sp in other_blocks:
        a, b = blocks.get(ol, sp)
        blocks[ol] = (min(a, sp[0]), max(b, sp[1]))
    span_lots: dict = {}           # the same span tagged to several lots
    for o in ents:
        if isinstance(o, dict) and _span(o):
            span_lots.setdefault((o.get("cls"), _span(o)), set()).add(_lot(o))
    out = []
    for e in ents:
        sp = _span(e)
        if not sp or _lot(e) != lot or e.get("cls") not in _DESCRIPTION_CLASSES:
            continue
        a, b = fd["span"]
        if a <= sp[0] and sp[1] <= b:
            continue
        txt, cls = _norm_ws(e.get("text")), e.get("cls")
        if _covered_by_text(txt, cls, fd):
            continue
        kind = (normalize_identifier_kind((e.get("attrs") or {}).get("kind"))[0]
                if cls == "identifier" else None)
        sharers = {ol: blocks[ol] for ol in span_lots.get((cls, sp), ())
                   if ol != lot and ol in blocks}
        if _outside_reason(sp, txt, cls, kind, lot, fd, other_blocks, md, sharers) is None:
            out.append(sp)
    return out


def widen(ents: list[dict], md: str) -> tuple[list[dict], dict]:
    """``(entities, report)`` with each lot's description stretched over the
    details it missed, as far as the rules above allow. ``report`` maps lot ->
    {"from": [s, e], "to": [s, e], "reached": n, "left": n} for each lot it
    changed or could not fully fix; entities are untouched when it changed
    nothing."""
    if not md or not ents:
        return ents, {}
    fds: dict[str, list[int]] = {}
    for i, e in enumerate(ents):
        if isinstance(e, dict) and e.get("cls") == "full_description" and _span(e):
            fds.setdefault(_lot(e), []).append(i)
    other_blocks = [(lot, _span(ents[i])) for lot, idx in fds.items() for i in idx]
    out = [dict(e) if isinstance(e, dict) else e for e in ents]
    report: dict = {}
    for lot, idx in fds.items():
        missed = _missed(ents, md, lot, idx, other_blocks)
        if not missed:
            continue
        own = set(idx)
        first = min(idx, key=lambda i: ents[i]["start"])
        last = max(idx, key=lambda i: ents[i]["end"])
        s0, e0 = ents[first]["start"], ents[last]["end"]
        s, e = s0, e0
        for a, b in sorted((sp for sp in missed if sp[1] > e0), key=lambda sp: sp[0]):
            if b <= e:
                continue
            if a - e > MAX_GAP or _blocked(e, b, lot, ents, own, md):
                break
            e = b
        for a, b in sorted((sp for sp in missed if sp[0] < s0), key=lambda sp: -sp[1]):
            if a >= s:
                continue
            if s - b > MAX_GAP or _blocked(a, s, lot, ents, own, md):
                break
            s = a
        if e > e0:
            tail = _line_end(md, e, len(md))
            if not _blocked(e, tail, lot, ents, own, md):
                e = tail
        if s < s0:
            head = _line_start(md, s, 0)
            if not _blocked(head, s, lot, ents, own, md):
                s = head
        reached = sum(1 for a, b in missed if s <= a and b <= e)
        if (s, e) == (s0, e0):
            report[lot] = {"from": [s0, e0], "to": [s0, e0], "reached": 0,
                           "left": len(missed)}
            continue
        for i, new_s, new_e in ((first, s, None), (last, None, e)):
            ent = out[i]
            ns = new_s if new_s is not None else ent["start"]
            ne = new_e if new_e is not None else ent["end"]
            if (ns, ne) == (ent["start"], ent["end"]):
                continue
            attrs = dict(ent.get("attrs") or {})
            attrs.setdefault("widened_from", [ent["start"], ent["end"]])
            out[i] = {**ent, "start": ns, "end": ne, "text": md[ns:ne], "attrs": attrs}
        report[lot] = {"from": [s0, e0], "to": [s, e], "reached": reached,
                       "left": len(missed) - reached}
    if not any(r["to"] != r["from"] for r in report.values()):
        return ents, report
    return assign_ids(out), report
