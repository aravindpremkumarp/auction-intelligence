"""Verify only uncertainty: a targeted re-read, then a short key-facts check.

1. ``uncertain`` names, per lot, the fields worth a second look: a key fact
   the first read did not fill although the lot's text has a word that could
   state it (pipeline/absence.CLUES), a value grounded only fuzzily, a value
   a consistency rule contested. ``reread`` asks the SAME cheap model for
   just those fields over the lot's own text (with the notice head and tail
   as context), proves the answers the usual way, and updates only those
   fields (``method=field_reread``).

2. ``verify`` asks a short KeyFactsRead for every lot in a chunk (a few
   hundred output tokens) and compares it with the first read. Agreement
   marks the fact VERIFIED; disagreement earns one more read, and the
   majority wins — a value that changes by vote is re-proved in the text
   before it replaces the first. No majority leaves the first value marked
   CONTESTED.

The model call is injected (``call(system, user, schema) -> parsed``) so the
logic is testable with a script and the orchestrator decides models/budget.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from pydantic import create_model

from api.review.grounding import _fold_text
from pipeline.absence import CLUES, MUST_HAVE_CLUES
from pipeline.key_entities import KEY_ATTR, KEY_CLASS, KEYS
from pipeline.reader import normalize as N
from pipeline.reader.convert import to_entities
from pipeline.reader.ground import Window, locate
from pipeline.reader.prompt import system_prompt, user_keyfacts, user_reread
from pipeline.reader.schema import KeyFacts, KeyFactsRead, LotRead, SegmentRead
from pipeline.reader.segment import ChunkText, Segment, Segmentation

Call = Callable[[str, str, type], object]

#: Which LotRead fields a key maps to, for the targeted re-read.
KEY_TO_FIELDS = {
    "reserve_price": ["reserve_price"], "auction_date": ["dates"],
    "property_type": ["property_type", "property_type_norm"],
    "location": ["location"], "extent": ["extents"],
    "full_description": ["description"], "possession_type": ["possession", "possession_type"],
}
_CLASS_TO_KEY = {v: k for k, v in KEY_CLASS.items()}


def _lot(e: dict) -> str:
    return str((e.get("attrs") or {}).get("lot_index") or "1")


def _filled(entities: list[dict], lot: str, key: str) -> dict | None:
    cls, attr = KEY_CLASS[key], KEY_ATTR[key]
    return next((e for e in entities if e.get("cls") == cls and _lot(e) == lot
                 and (attr is None or (e.get("attrs") or {}).get(attr))), None)


def uncertain(entities: list[dict], seg: Segmentation, md: str,
              not_stated: list[tuple[str, str]] = ()) -> dict[str, list[str]]:
    """{lot_index: [LotRead field names]} worth a targeted re-read."""
    out: dict[str, set[str]] = {}
    lots = {_lot(e) for e in entities if e.get("cls") not in
            ("secured_creditor", "contact", "emd_account", "full_terms")}
    for lot in sorted(lots, key=lambda x: int(x) if x.isdigit() else 0):
        s = seg.segments[int(lot) - 1] if lot.isdigit() and int(lot) - 1 < len(seg.segments) else None
        text = md[s.start:s.end] if s and not seg.whole else md
        for key in KEYS:
            if _filled(entities, lot, key) is not None:
                continue
            rx = CLUES.get(key) or MUST_HAVE_CLUES.get(key)
            if rx is None or rx.search(text):
                out.setdefault(lot, set()).update(KEY_TO_FIELDS[key])
    for e in entities:
        a = e.get("attrs") or {}
        if a.get("evidence") not in ("FUZZY_GROUNDED", "CONTESTED"):
            continue
        rule = str(a.get("rule") or "")
        fields: set[str] = set()
        if e.get("cls") == "auction_terms":
            # an EMD finding is about the EMD relative to the reserve: both
            if "reserve_price_num" in rule or "emd_num" in rule or not rule:
                fields.update(("reserve_price", "emd"))
            if "_dt" in rule or not rule:
                fields.add("dates")
        elif e.get("cls") == "property":
            fields.update(KEY_TO_FIELDS["property_type"] + KEY_TO_FIELDS["possession_type"])
        elif e.get("cls") in _CLASS_TO_KEY:
            fields.update(KEY_TO_FIELDS[_CLASS_TO_KEY[e["cls"]]])
        if fields:
            out.setdefault(_lot(e), set()).update(fields)
    return {k: sorted(v) for k, v in out.items()}


def reread_schema(fields: list[str]):
    """A LotRead with only ``fields``, for a focused read."""
    spec = {f: (LotRead.model_fields[f].annotation, LotRead.model_fields[f])
            for f in fields if f in LotRead.model_fields}
    return create_model("FieldReread", __base__=LotRead.__bases__[0], **spec)


def reread(entities: list[dict], lot_index: str, fields: list[str], seg: Segmentation,
           chunk_for_lot: ChunkText, md: str, call: Call, *, blocks=None, tables=None,
           prompt_hash: str = "") -> tuple[list[dict], dict]:
    """``(entities, report)`` with ``fields`` of ``lot_index`` re-read and
    replaced. Only entities of the re-read fields' classes move."""
    schema = reread_schema(fields)
    parsed = call(system_prompt("reread"), user_reread(chunk_for_lot.text, fields), schema)
    lot = LotRead.model_validate(parsed.model_dump() if hasattr(parsed, "model_dump") else parsed)
    i = int(lot_index) - 1
    s = seg.segments[i] if i < len(seg.segments) else Segment(0, len(md), 0)
    conv = to_entities(None, [(s, lot)], md, blocks=blocks, tables=tables, prompt_hash=prompt_hash)
    classes = {KEY_CLASS[k] for k, fs in KEY_TO_FIELDS.items() if set(fs) & set(fields)}
    if "emd" in fields:
        classes.add("auction_terms")
    fresh = [e for e in conv.entities if e["cls"] in classes]
    for e in fresh:
        e["attrs"]["lot_index"] = lot_index
        e["attrs"]["method"] = "field_reread"
    kept = []
    for e in entities:
        if _lot(e) == lot_index and e.get("cls") in classes:
            tgt = next((f for f in fresh if f["cls"] == e["cls"]), None)
            if tgt is not None and e["cls"] in ("auction_terms", "property", "location"):
                # one entity per lot for these classes: the re-read's attrs
                # win, the ones it did not touch stay
                for k, v in (e.get("attrs") or {}).items():
                    if k not in ("evidence", "anchor", "method", "rule", "verified"):
                        tgt["attrs"].setdefault(k, v)
                continue
            if tgt is not None:
                continue
        kept.append(e)
    return kept + fresh, {"lot": lot_index, "fields": fields, "replaced": len(fresh)}


# ── the key-facts vote ────────────────────────────────────────────────────────
VOTE_KEYS = ("reserve_price_num", "emd_num", "auction_start_dt", "property_type",
             "possession_type", "extent", "village", "taluk")


def _facts_of(entities: list[dict], lot: str) -> dict[str, str | None]:
    t = _filled(entities, lot, "reserve_price") or next(
        (e for e in entities if e["cls"] == "auction_terms" and _lot(e) == lot), None)
    p = next((e for e in entities if e["cls"] == "property" and _lot(e) == lot), None)
    x = next((e for e in entities if e["cls"] == "extent" and _lot(e) == lot), None)
    l = next((e for e in entities if e["cls"] == "location" and _lot(e) == lot), None)
    ta = (t or {}).get("attrs") or {}
    pa = (p or {}).get("attrs") or {}
    la = (l or {}).get("attrs") or {}
    return {"reserve_price_num": ta.get("reserve_price_num"), "emd_num": ta.get("emd_num"),
            "auction_start_dt": ta.get("auction_start_dt"),
            "property_type": pa.get("property_type"), "possession_type": pa.get("possession_type"),
            "extent": (x or {}).get("text"), "village": la.get("village"), "taluk": la.get("taluk")}


def _money_of(fact) -> "N.Norm":
    # "illegible" is the model's hint; code still tries the narrow repairs.
    if fact.quote and fact.status in ("found", "illegible"):
        return N.money(fact.quote, fact.unit)
    return N.NONE


def _facts_of_read(k: KeyFacts) -> dict[str, str | None]:
    rp = _money_of(k.reserve_price)
    em = _money_of(k.emd)
    dt = N.date(k.auction_start.quote, k.auction_start.iso) if k.auction_start and k.auction_start.status == "found" else N.NONE
    poss = N.possession(k.possession.quote) if k.possession.status == "found" else N.NONE
    return {"reserve_price_num": str(rp.value) if rp.state == "ok" else None,
            "emd_num": str(em.value) if em.state == "ok" else None,
            "auction_start_dt": dt.value if dt.state == "ok" else None,
            "property_type": k.property_type.quote if k.property_type.status == "found" else None,
            "possession_type": poss.value if poss.state == "ok" else k.possession_type,
            "extent": k.extent_quote, "village": k.village, "taluk": k.taluk}


def _agree(key: str, a, b) -> bool:
    if a in (None, "") or b in (None, ""):
        return a in (None, "") and b in (None, "")
    if key in ("reserve_price_num", "emd_num"):
        return str(a) == str(b)
    if key == "auction_start_dt":
        return str(a)[:10] == str(b)[:10]
    fa, fb = _fold_text(str(a)), _fold_text(str(b))
    return fa == fb or fa in fb or fb in fa


def _apply_value(entities: list[dict], lot: str, key: str, value, quote: str | None,
                 win: Window) -> bool:
    """Replace a key fact by vote — only when its quote is on the page."""
    if key in ("reserve_price_num", "emd_num", "auction_start_dt"):
        e = next((x for x in entities if x["cls"] == "auction_terms" and _lot(x) == lot), None)
    elif key in ("property_type", "possession_type"):
        e = next((x for x in entities if x["cls"] == "property" and _lot(x) == lot), None)
    elif key in ("village", "taluk"):
        e = next((x for x in entities if x["cls"] == "location" and _lot(x) == lot), None)
    else:
        return False
    # A vote may REPLACE a value with one it can point to on the page; it may
    # never DELETE one. Two check reads agreeing on "nothing" outvoted a
    # correct reserve price on gold 750348 and erased it.
    if e is None or value in (None, "") or not quote or locate(win, quote) is None:
        return False
    e["attrs"][key] = value
    e["attrs"]["method"] = "verify_vote"
    return True


def verify(entities: list[dict], seg: Segmentation, chunk: ChunkText, md: str, call: Call,
           *, max_reads: int = 2) -> dict:
    """The key-facts vote over one chunk's lots. Mutates ``entities``
    (evidence marks, voted values); returns a report."""
    lots = [str(i + 1) for i in chunk.segment_ids] if not seg.whole else sorted(
        {_lot(e) for e in entities if e["cls"] not in ("secured_creditor", "contact", "emd_account", "full_terms")},
        key=lambda s: int(s) if s.isdigit() else 0)
    read1 = call(system_prompt("keyfacts"), user_keyfacts(chunk.text, len(lots)), KeyFactsRead)
    rows = list(getattr(read1, "lots", []) or [])
    report = {"lots": len(lots), "agreed": 0, "contested": 0, "voted": 0, "reads": 1}
    pending: dict[str, dict[str, tuple]] = {}
    for lot, kf in zip(lots, rows):
        first = _facts_of(entities, lot)
        second = _facts_of_read(kf)
        for key in VOTE_KEYS:
            if _agree(key, first[key], second[key]):
                _mark_verified(entities, lot, key)
                report["agreed"] += 1
            else:
                pending.setdefault(lot, {})[key] = (first[key], second[key], kf)
    if pending and max_reads >= 2:
        read3 = call(system_prompt("keyfacts"), user_keyfacts(chunk.text, len(lots)), KeyFactsRead)
        report["reads"] = 2
        rows3 = list(getattr(read3, "lots", []) or [])
        third_by_lot = {lot: _facts_of_read(k) for lot, k in zip(lots, rows3)}
    else:
        third_by_lot = {}
    for lot, keys in pending.items():
        i = int(lot) - 1
        s = seg.segments[i] if i < len(seg.segments) and not seg.whole else None
        win = Window(md, s.start, s.end) if s else Window(md)
        for key, (v1, v2, kf) in keys.items():
            v3 = third_by_lot.get(lot, {}).get(key, "__none__")
            if v3 != "__none__" and _agree(key, v1, v3):
                _mark_verified(entities, lot, key)
                report["agreed"] += 1
            elif v3 != "__none__" and _agree(key, v2, v3):
                quote = _quote_for(kf, key)
                if _apply_value(entities, lot, key, v2, quote, win):
                    _mark_verified(entities, lot, key)
                    report["voted"] += 1
                else:
                    _mark_contested(entities, lot, key)
                    report["contested"] += 1
            else:
                _mark_contested(entities, lot, key)
                report["contested"] += 1
    return report


def _quote_for(kf: KeyFacts, key: str) -> str | None:
    return {"reserve_price_num": kf.reserve_price.quote, "emd_num": kf.emd.quote,
            "auction_start_dt": kf.auction_start.quote if kf.auction_start else None,
            "property_type": kf.property_type.quote, "possession_type": kf.possession.quote,
            "extent": kf.extent_quote, "village": kf.village, "taluk": kf.taluk}.get(key)


def _entity_for(entities: list[dict], lot: str, key: str) -> dict | None:
    cls = {"reserve_price_num": "auction_terms", "emd_num": "auction_terms",
           "auction_start_dt": "auction_terms", "property_type": "property",
           "possession_type": "property", "extent": "extent", "village": "location",
           "taluk": "location"}[key]
    return next((e for e in entities if e["cls"] == cls and _lot(e) == lot), None)


def _mark_verified(entities: list[dict], lot: str, key: str) -> None:
    e = _entity_for(entities, lot, key)
    if e is None:
        return
    a = e["attrs"]
    v = [x for x in str(a.get("verified") or "").split(",") if x]
    if key not in v:
        v.append(key)
    a["verified"] = ",".join(v)
    if a.get("evidence") in (None, "EXPLICIT", "FUZZY_GROUNDED", "INHERITED"):
        a["evidence"] = "VERIFIED"


def _mark_contested(entities: list[dict], lot: str, key: str) -> None:
    e = _entity_for(entities, lot, key)
    if e is None:
        return
    a = e["attrs"]
    a["evidence"] = "CONTESTED"
    rules = [r for r in str(a.get("rule") or "").split(";") if r]
    tag = f"{key}:vote_disagreed"
    if tag not in rules:
        rules.append(tag)
    a["rule"] = ";".join(rules)
    a.pop("verified", None)


__all__ = ["uncertain", "reread", "reread_schema", "verify", "VOTE_KEYS", "KEY_TO_FIELDS"]
