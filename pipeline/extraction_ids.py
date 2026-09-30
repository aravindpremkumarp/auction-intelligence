"""Stable entity ids, and reviewer corrections that survive a re-read.

Why
---
A stored entity's ``id`` used to be its position in the list, and reviewer
corrections (``Document.extraction_corrections_json``) are keyed by that id.
A re-extraction rewrote the list and kept the corrections, so a person's fix
to entity 7 silently attached to whatever the new read put seventh — human
input corrupting machine output at exactly the moment quality improved
(docs/extraction-pipeline-audit-2026-08.md, R2).

What
----
* :func:`stable_id` — ``sha1(cls | lot_index | start | end | folded text)[:12]``.
  Identical re-reads give identical ids; a moved or reworded entity gets a
  new one, which is what makes the next step honest.
* :func:`reanchor_corrections` — given the old entities, the new entities
  and the corrections, move each correction onto the new entity it belongs
  to (same class and lot, and the span overlaps by half, or the text is the
  same after folding, or nearly so); carry ``add:*`` entities as they are
  (their spans re-anchor with everything else); move ``absent:`` /
  ``unfound:`` marks through an old→new lot map built from where each lot's
  full_description now sits. Anything that matches nothing becomes
  ``orphaned:<old id>``, carrying the old entity's class, text and span, so
  the review page can show it and a person can re-place it. Nothing is
  silently applied to the wrong entity, and nothing a person wrote is lost.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

from rapidfuzz import fuzz

from api.review.grounding import _fold_text

ORPHAN_PREFIX = "orphaned:"
ADD_PREFIX = "add:"
_MARK_RE = re.compile(r"^(?P<kind>absent|unfound):(?P<lot>[^:]+):(?P<key>[a-z_]+)$")

SPAN_OVERLAP_MIN = 0.5
SPAN_TEXT_MIN = 70.0
TEXT_RATIO_MIN = 90.0


def _lot(e: dict) -> str:
    return str((e.get("attrs") or {}).get("lot_index") or "1")


def stable_id(e: dict) -> str:
    key = "|".join([str(e.get("cls") or ""), _lot(e), str(e.get("start")),
                    str(e.get("end")), _fold_text(e.get("text") or "")])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def assign_ids(ents: list[dict]) -> list[dict]:
    """``ents`` (new dicts) with stable ids; a collision gets a ``-2`` suffix."""
    seen: Counter = Counter()
    out = []
    for e in ents:
        if not isinstance(e, dict):
            continue
        base = stable_id(e)
        seen[base] += 1
        out.append({**e, "id": base if seen[base] == 1 else f"{base}-{seen[base]}"})
    return out


# ── matching old entities to new ─────────────────────────────────────────────

def _overlap(a: dict, b: dict) -> float:
    if a.get("start") is None or b.get("start") is None:
        return 0.0
    s = max(a["start"], b["start"])
    e = min(a["end"], b["end"])
    inter = max(0, e - s)
    shorter = min(a["end"] - a["start"], b["end"] - b["start"])
    return inter / shorter if shorter > 0 else 0.0


def _same_text(a: dict, b: dict) -> bool:
    ta, tb = _fold_text(a.get("text") or ""), _fold_text(b.get("text") or "")
    if not ta or not tb:
        return False
    return ta == tb or fuzz.ratio(ta, tb) >= TEXT_RATIO_MIN


def lot_map(old_ents: list[dict], new_ents: list[dict]) -> dict[str, str]:
    """old lot_index -> new lot_index, by where each lot's full_description
    (else any grounded entity) now sits. A lot with no counterpart is absent
    from the map."""
    def spans(ents: list[dict]) -> dict[str, tuple[int, int]]:
        best: dict[str, tuple[int, int]] = {}
        for cls_pref in ("full_description", None):
            for e in ents:
                if e.get("start") is None:
                    continue
                if cls_pref and e.get("cls") != cls_pref:
                    continue
                li = _lot(e)
                if li in best:
                    continue
                best[li] = (e["start"], e["end"])
        return best
    old, new = spans(old_ents), spans(new_ents)
    out: dict[str, str] = {}
    for oli, (os_, oe) in old.items():
        cand = []
        for nli, (ns, ne) in new.items():
            inter = max(0, min(oe, ne) - max(os_, ns))
            if inter > 0:
                cand.append((inter, nli))
        if cand:
            out[oli] = max(cand)[1]
    return out


def match_entity(old: dict, new_ents: list[dict], lots: dict[str, str]) -> dict | None:
    """The new entity ``old`` became, or None."""
    want_lot = lots.get(_lot(old), _lot(old))
    same = [n for n in new_ents if n.get("cls") == old.get("cls") and _lot(n) == want_lot]
    # A span match alone is not enough: the new read can put a DIFFERENT
    # value at the same place (another borrower's name in the same cell), and
    # a correction written against the old text must not land on it.
    by_span = [(_overlap(old, n), n) for n in same
               if fuzz.partial_ratio(_fold_text(old.get("text") or ""),
                                     _fold_text(n.get("text") or "")) >= SPAN_TEXT_MIN]
    by_span = [(o, n) for o, n in by_span if o >= SPAN_OVERLAP_MIN]
    if by_span:
        return max(by_span, key=lambda x: x[0])[1]
    by_text = [n for n in same if _same_text(old, n)]
    if len(by_text) == 1:
        return by_text[0]
    if by_text:  # several with the same text: the nearest span
        return min(by_text, key=lambda n: abs((n.get("start") or 0) - (old.get("start") or 0)))
    return None


def reanchor_corrections(old_ents: list[dict], new_ents: list[dict],
                         corrections: dict | None) -> tuple[dict, dict]:
    """``(corrections', report)`` for a re-read. See the module docstring."""
    corrections = corrections if isinstance(corrections, dict) else {}
    old_by_id = {}
    for i, e in enumerate(old_ents or []):
        if isinstance(e, dict):
            old_by_id[str(e.get("id") or i)] = e
            old_by_id.setdefault(str(i), e)
    lots = lot_map(old_ents or [], new_ents or [])
    out: dict = {}
    report: Counter = Counter()
    for key, val in corrections.items():
        k = str(key)
        if k.startswith(ORPHAN_PREFIX):
            out[k] = val                     # never dropped, never re-applied
            report["orphaned_kept"] += 1
            continue
        if k.startswith(ADD_PREFIX):
            v = val
            if isinstance(val, dict) and isinstance(val.get("attrs"), dict):
                li = str(val["attrs"].get("lot_index") or "1")
                if li in lots and lots[li] != li:
                    v = {**val, "attrs": {**val["attrs"], "lot_index": lots[li]}}
            out[k] = v
            report["kept"] += 1
            continue
        m = _MARK_RE.match(k)
        if m:
            li = m.group("lot")
            if li in lots:
                out[f"{m.group('kind')}:{lots[li]}:{m.group('key')}"] = val
                report["moved" if lots[li] != li else "kept"] += 1
            else:
                out[f"{ORPHAN_PREFIX}{k}"] = val
                report["orphaned"] += 1
            continue
        old = old_by_id.get(k)
        if old is None:
            out[f"{ORPHAN_PREFIX}{k}"] = val
            report["orphaned"] += 1
            continue
        new = match_entity(old, new_ents or [], lots)
        if new is None:
            orphan = dict(val) if isinstance(val, dict) else {"value": val}
            orphan["orphaned_from"] = {"id": k, "cls": old.get("cls"),
                                       "text": old.get("text"), "start": old.get("start"),
                                       "end": old.get("end"), "lot_index": _lot(old)}
            out[f"{ORPHAN_PREFIX}{k}"] = orphan
            report["orphaned"] += 1
            continue
        nid = str(new.get("id"))
        out[nid] = val
        report["moved" if nid != k else "kept"] += 1
    return out, dict(report)


def carry_corrections(old_json: str | None, corr_json: str | None,
                      new_ents: list[dict]) -> tuple[str, dict]:
    """The JSON to store beside a fresh read: the old corrections re-anchored
    onto ``new_ents``. Both inputs are the raw stored strings."""
    try:
        old = json.loads(old_json) if old_json else []
    except (TypeError, ValueError):
        old = []
    try:
        corr = json.loads(corr_json) if corr_json else {}
    except (TypeError, ValueError):
        corr = {}
    if not corr:
        return "{}", {}
    moved, report = reanchor_corrections(old if isinstance(old, list) else [],
                                         new_ents, corr)
    return json.dumps(moved, ensure_ascii=False), report


def orphans(corrections: dict | None) -> list[dict]:
    """The orphaned corrections, for the review page."""
    if not isinstance(corrections, dict):
        return []
    out = []
    for k, v in corrections.items():
        if not str(k).startswith(ORPHAN_PREFIX):
            continue
        item = {"key": str(k)[len(ORPHAN_PREFIX):]}
        if isinstance(v, dict):
            item.update({kk: vv for kk, vv in v.items() if kk in ("value", "by", "at", "orphaned_from", "rule")})
        out.append(item)
    return out


__all__ = ["stable_id", "assign_ids", "reanchor_corrections", "carry_corrections",
           "lot_map", "match_entity", "orphans", "ORPHAN_PREFIX"]
