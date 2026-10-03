"""Field-level counts for one read: what was found, how, and at what cost.

Per class and per key fact: how many values are EXPLICIT / INHERITED /
FUZZY_GROUNDED / CONTESTED / NOT_STATED / ILLEGIBLE / VERIFIED, how many
quotes were DROPPED (not on the page), the anchor mix, calls and tokens, and
the two costs that matter — per filled key fact and per VERIFIED key fact.
One JSON object per read; the shadow report and evals aggregate them, and
the write stamps a compact form on the document.
"""
from __future__ import annotations

from collections import Counter

from pipeline.key_entities import KEY_ATTR, KEY_CLASS, KEYS

STATES = ("EXPLICIT", "INHERITED", "FUZZY_GROUNDED", "CONTESTED", "NOT_STATED",
          "ILLEGIBLE", "VERIFIED", "DROPPED")


def _lot(e: dict) -> str:
    return str((e.get("attrs") or {}).get("lot_index") or "1")


def summarise(entities: list[dict], dropped: list[dict], *, usage: dict | None = None,
              cost_usd: float | None = None, calls: int = 0, seconds: float | None = None,
              segmentation: dict | None = None, not_stated: list | None = None) -> dict:
    by_class: dict[str, Counter] = {}
    anchors: Counter = Counter()
    for e in entities:
        a = e.get("attrs") or {}
        by_class.setdefault(e.get("cls") or "?", Counter())[a.get("evidence") or "EXPLICIT"] += 1
        anchors[a.get("anchor") or ("exact" if e.get("start") is not None else "none")] += 1
    for d in dropped:
        by_class.setdefault(d.get("cls") or "?", Counter())["DROPPED"] += 1

    lots = {_lot(e) for e in entities if e.get("cls") not in
            ("secured_creditor", "contact", "emd_account", "full_terms")} or {"1"}
    key_filled = key_verified = 0
    key_states: dict[str, Counter] = {k: Counter() for k in KEYS}
    for lot in lots:
        for key in KEYS:
            cls, attr = KEY_CLASS[key], KEY_ATTR[key]
            hit = next((e for e in entities if e.get("cls") == cls and _lot(e) == lot
                        and (attr is None or (e.get("attrs") or {}).get(attr))), None)
            if hit is None:
                key_states[key]["missing"] += 1
                continue
            key_filled += 1
            st = (hit.get("attrs") or {}).get("evidence") or "EXPLICIT"
            verified = attr in str((hit.get("attrs") or {}).get("verified") or "") if attr else st == "VERIFIED"
            if st == "VERIFIED" or verified:
                key_verified += 1
            key_states[key][st] += 1
    for lot, key in (not_stated or []):
        key_states.setdefault(key, Counter())["NOT_STATED"] += 1

    return {
        "lots": len(lots),
        "entities": len(entities),
        "dropped": len(dropped),
        "by_class": {c: dict(cnt) for c, cnt in sorted(by_class.items())},
        "anchors": dict(anchors),
        "key_facts": {"filled": key_filled, "verified": key_verified,
                      "total": len(lots) * len(KEYS),
                      "states": {k: dict(v) for k, v in key_states.items()}},
        "calls": calls,
        "usage": dict(usage or {}),
        "cost_usd": cost_usd,
        "cost_per_filled_key_fact": (round(cost_usd / key_filled, 5)
                                     if cost_usd is not None and key_filled else None),
        "cost_per_verified_key_fact": (round(cost_usd / key_verified, 5)
                                       if cost_usd is not None and key_verified else None),
        "seconds": seconds,
        "segmentation": segmentation or {},
    }


def compact(t: dict) -> dict:
    """The few numbers worth stamping on the document."""
    return {"entities": t["entities"], "dropped": t["dropped"],
            "key_filled": t["key_facts"]["filled"], "key_verified": t["key_facts"]["verified"],
            "key_total": t["key_facts"]["total"], "calls": t["calls"],
            "cost_usd": t["cost_usd"], "strategy": (t.get("segmentation") or {}).get("strategy")}


__all__ = ["summarise", "compact", "STATES"]
