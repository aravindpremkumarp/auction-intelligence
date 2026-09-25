"""Fill a stored extraction's missing key facts with short, targeted reads.

A full extraction sends the whole guide (~24k characters) and nine worked
examples (~47k) with every call — about 18k tokens before the notice itself.
That is right for a first read, which has to find everything. It is waste for
a second read that needs one lot's reserve price.

This module asks only for what is missing:

1. ``gaps`` lists, per lot, the key facts (pipeline/key_entities) the stored
   extraction lacks.
2. ``excerpt`` cuts that lot's own text: the whole notice for a single-lot
   notice; for a multi-lot one, the lot as pipeline/lot_chunks finds it (with
   its table header), else a window around the lot's stored spans.
3. ``lean_prompt`` / ``lean_example`` build a prompt from the guide's own
   definitions of just the needed classes and rules, and one example cut down
   to those classes — a few hundred tokens, not eighteen thousand.
4. ``fill`` runs one read per lot with gaps and folds what comes back into the
   stored read with pipeline/keep_better.merge, which only ever adds a fact a
   lot lacks. The caller saves the result through the keep-better gate.
"""
from __future__ import annotations

import re
from typing import Callable

from pipeline.keep_better import merge
from pipeline.key_entities import KEY_ATTR, KEY_CLASS, KEY_LABELS, key_checklist
from pipeline.lot_chunks import (
    HEAD_CAP, TAIL_CAP, _compose, _Text, _to_notice, plan_chunks,
)

#: A multi-lot notice the splitter cannot cut: this much text either side of
#: the lot's stored spans, enough to reach a price line or a boundary clause.
WINDOW = 1500

#: Rules from the full guide that a key cannot be read correctly without.
_KEY_RULES = {
    "possession_type": ("- POSSESSION",),
    "property_type": ("- APARTMENTS / FLATS",),
    "extent": ("- APARTMENTS / FLATS",),
    "location": ("- REGISTRATION DISTRICTS",),
}


def _lot(e: dict) -> str:
    return str((e.get("attrs") or {}).get("lot_index") or "1")


def gaps(ents: list[dict]) -> dict[str, list[str]]:
    """lot_index -> the key facts the extraction lacks for that lot. Only lots
    the extraction holds: a lot missing entirely is a missing-lot problem, not a
    gap to fill."""
    out: dict[str, list[str]] = {}
    for lot in key_checklist(ents)["lots"]:
        if not lot.get("extracted"):
            continue
        miss = [k for k, c in lot["cells"].items() if c["status"] == "missing"]
        if miss:
            out[str(lot["lot_index"])] = miss
    return out


# ── the lean prompt ──────────────────────────────────────────────────────────

def _guide() -> str:
    from pipeline import langextract_examples as LX
    return LX._LANGEXTRACT_GUIDE


def _class_blocks(guide: str) -> dict[str, str]:
    """Each class's own definition, as the full guide words it."""
    start = guide.index("extraction classes")
    end = guide.index("CONVENTIONS:")
    blocks: dict[str, list[str]] = {}
    cur = None
    for line in guide[start:end].splitlines()[1:]:
        m = re.match(r"- (\w+)\s*:", line)
        if m:
            cur = m.group(1)
            blocks[cur] = [line]
        elif cur and line.startswith("  "):
            blocks[cur].append(line)
        else:
            cur = None
    return {k: "\n".join(v) for k, v in blocks.items()}


def _bullet(guide: str, head: str) -> str:
    """One bullet of the guide's rules, continuation lines included."""
    i = guide.find(head)
    if i < 0:
        return ""
    out = []
    for n, line in enumerate(guide[i:].splitlines()):
        if n and (line.startswith("- ") or not line.strip()):
            break
        out.append(line)
    return "\n".join(out)


def lean_prompt(keys: list[str], hint: str | None = None) -> str:
    guide = _guide()
    classes = sorted({KEY_CLASS[k] for k in keys})
    blocks = _class_blocks(guide)
    rules = []
    for k in keys:
        for head in _KEY_RULES.get(k, ()):
            b = _bullet(guide, head)
            if b and b not in rules:
                rules.append(b)
    wanted = ", ".join(KEY_LABELS[k].lower() for k in keys)
    parts = [
        "You fill gaps in an extraction of ONE lot of an Indian bank auction "
        "sale notice. Earlier reads captured everything else; extract ONLY the "
        f"classes below, for this lot only, each tagged lot_index=1.\n"
        f"Still needed for this lot: {wanted}.",
        "CLASSES:\n" + "\n".join(blocks[c] for c in classes if c in blocks),
        "RULES:\n"
        "- extraction_text MUST be copied verbatim from the excerpt and be ONE "
        "contiguous run of characters; put every other value in attrs.\n"
        "- Money -> integer rupees in attrs (Rs.9,50,000 -> 950000). Dates ISO "
        "8601 with time when given.\n"
        "- OMIT any attribute that is absent — never output \"null\"/\"NA\"/"
        "empty. If the excerpt does not state a value, emit nothing for it.\n"
        "- In a table, a lot's reserve price may sit in an unlabelled cell of "
        "its row; the column header names it."
        + ("\n" + "\n".join(rules) if rules else ""),
    ]
    if hint:
        parts.append(hint)
    return "\n\n".join(parts)


def lean_example(classes: set[str]):
    """The single-lot worked example cut down to ``classes``: its extractions
    of those classes, and only as much of its text as holds them."""
    import langextract as lx

    from pipeline import langextract_examples as LX
    ex = LX.SINGLE_EXAMPLE
    keep = [x for x in ex.extractions if x.extraction_class in classes
            and ex.text.find(x.extraction_text) >= 0]
    if not keep:
        keep = [x for x in ex.extractions if x.extraction_class == "auction_terms"]
    spans = [(ex.text.find(x.extraction_text), len(x.extraction_text)) for x in keep]
    lo = max(0, min(s for s, _ in spans) - 150)
    hi = min(len(ex.text), max(s + n for s, n in spans) + 150)
    text = ex.text[lo:hi]
    return lx.data.ExampleData(text=text, extractions=[
        lx.data.Extraction(extraction_class=x.extraction_class,
                           extraction_text=x.extraction_text,
                           attributes={**(x.attributes or {}), "lot_index": "1"})
        for x in keep if x.extraction_text in text])


# ── the lot's own text ───────────────────────────────────────────────────────

def _cut(md: str, ranges: list[tuple[int, int]]):
    """The text of ``ranges`` of ``md`` (merged, in order), and a map back."""
    merged: list[list[int]] = []
    for s, e in sorted(r for r in ranges if r[1] > r[0]):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    parts, segments, off = [], [], 0
    for s, e in merged:
        if parts:
            parts.append("\n\n")
            off += 2
        parts.append(md[s:e])
        segments.append((off, s, e - s))
        off += e - s
    t = _Text("".join(parts), segments, [], len(md))
    return t.text, (lambda s, e: _to_notice(t, s, e))


def _frame(md: str, body: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """``body`` with the notice's head and tail around it. A notice often
    states a fact once for every lot — "the physical possession of which has
    been taken", the auction date under the schedule — and a lot's own text
    alone would then look as if it never states it."""
    lo = min(s for s, _ in body)
    hi = max(e for _, e in body)
    return [(0, min(HEAD_CAP, lo)), *body, (max(hi, len(md) - TAIL_CAP), len(md))]


def excerpt(md: str, ents: list[dict], lot: str, n_lots: int,
            expected_lot_count: int | None = None):
    """``(text, back, hint)`` for one lot, or None when it cannot be found.

    The lot's own text framed by the notice's head and tail. ``back(start,
    end)`` maps offsets in ``text`` to offsets in ``md``; ``hint`` names the
    lot when the text may hold its neighbours too.
    """
    if n_lots <= 1:
        return md, (lambda s, e: (s, e)), None
    plan = plan_chunks(md, expected_lot_count)
    if plan is not None and lot.isdigit() and 1 <= int(lot) <= len(plan.lots):
        t = _compose(md, plan, [int(lot) - 1])
        body = [(orig, orig + n) for _, orig, n in t.segments]
        text, back = _cut(md, _frame(md, body))
        return text, back, None
    mine = [e for e in ents if _lot(e) == lot and e.get("start") is not None
            and e.get("end") is not None]
    if not mine:
        return None
    lo = max(0, min(e["start"] for e in mine) - WINDOW)
    hi = min(len(md), max(e["end"] for e in mine) + WINDOW)
    anchor = next((e for e in mine if e.get("cls") in
                   ("full_description", "property", "borrower")), mine[0])
    start = " ".join(str(anchor.get("text") or "").split())[:100]
    hint = (f"This excerpt may show neighbouring lots too. Extract only for the "
            f"lot that contains: \"{start}\". A fact stated once for every lot "
            f"(in the notice's opening or closing text) applies to this lot too.")
    text, back = _cut(md, _frame(md, [(lo, hi)]))
    return text, back, hint


# ── facts stated once for every lot ──────────────────────────────────────────

#: Keys a notice may state once, above its lots, for all of them ("the
#: physical possession of which has been taken"). The auction date is
#: inherited by pipeline/key_entities.key_checklist already.
SHARED_KEYS = ("possession_type",)


def inherit_shared(ents: list[dict]) -> list[dict]:
    """``ents`` with a fact stated in the notice's header copied to every lot
    that lacks it. "In the header" means its span starts before any lot's
    description does. A read that applied the sentence to some lots and not
    others would otherwise leave the rest looking as if the notice never said
    it."""
    starts = [e["start"] for e in ents if e.get("cls") == "full_description"
              and e.get("start") is not None]
    if not starts:
        return ents
    first = min(starts)
    lots = {_lot(e) for e in ents}
    donor: list[dict] = []
    for key in SHARED_KEYS:
        cls, attr = KEY_CLASS[key], KEY_ATTR[key]
        src = next((e for e in ents if e.get("cls") == cls
                    and e.get("start") is not None and e["start"] < first
                    and (e.get("attrs") or {}).get(attr)), None)
        if src is None:
            continue
        for lot in lots:
            donor.append({**src, "attrs": {**src["attrs"], "lot_index": lot,
                                           "from_notice_header": "true"}})
    return merge(ents, donor) if donor else ents


# ── filling ──────────────────────────────────────────────────────────────────

def plan(md: str, stored: list[dict], skip: set[tuple[str, str]] = frozenset(),
         expected_lot_count: int | None = None
         ) -> tuple[dict[str, list[str]], dict[tuple[str, str], str]]:
    """``(todo, marks)``: the gaps worth a read, and the ones that are not.

    ``skip`` holds (lot, key) already marked — by a person or an earlier run —
    and is left out. A gap whose lot text has no word that could state it
    (pipeline/absence.no_clue) goes to ``marks`` as absent with no read.
    """
    from pipeline.absence import RULE_NO_CLUE, no_clue
    n_lots = max(len({_lot(e) for e in stored}), 1)
    todo: dict[str, list[str]] = {}
    marks: dict[tuple[str, str], str] = {}
    for lot, keys in gaps(stored).items():
        keys = [k for k in keys if (lot, k) not in skip]
        if not keys:
            continue
        cut = excerpt(md, stored, lot, n_lots, expected_lot_count)
        text = cut[0] if cut else md
        for k in keys:
            if no_clue(text, k):
                marks[(lot, k)] = RULE_NO_CLUE
            else:
                todo.setdefault(lot, []).append(k)
    return todo, marks


def fill(md: str, stored: list[dict], read: Callable[..., list[dict]],
         expected_lot_count: int | None = None,
         max_lots: int | None = None,
         todo: dict[str, list[str]] | None = None) -> tuple[list[dict], dict]:
    """``stored`` with its gaps filled from short reads, and a report.

    ``read(text, keys, hint)`` returns stored-shape entities with offsets into
    ``text``. One read per lot with gaps (``todo``, default every gap), at most
    ``max_lots`` of them. The result is ``keep_better.merge(stored, donor)``:
    nothing ``stored`` has is touched. ``report["read_lots"]`` lists the lots
    whose read came back, so a caller never takes a failed read as "not found".
    """
    todo = gaps(stored) if todo is None else todo
    lots = sorted(todo, key=lambda s: (len(s), s))[:max_lots]
    n_lots = max(len({_lot(e) for e in stored}), 1)
    donor: list[dict] = []
    report = {"lots_with_gaps": len(todo), "reads": 0, "failed": 0,
              "read_lots": []}
    for lot in lots:
        keys = todo[lot]
        cut = excerpt(md, stored, lot, n_lots, expected_lot_count)
        if cut is None:
            continue
        text, back, hint = cut
        report["reads"] += 1
        try:
            got = read(text, keys, hint)
        except Exception:  # one failed read must not cost the others
            report["failed"] += 1
            continue
        report["read_lots"].append(lot)
        classes = {KEY_CLASS[k] for k in keys}
        for e in got:
            if e.get("cls") not in classes:
                continue
            s, t = back(e.get("start"), e.get("end"))
            donor.append({**e, "start": s, "end": t,
                          "attrs": {**(e.get("attrs") or {}), "lot_index": lot,
                                    "gap_fill": "true"}})
    return merge(stored, donor), report
