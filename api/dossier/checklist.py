"""
api/dossier/checklist.py
------------------------
Pure functions that turn "which doc types does this dossier have?" into the
have/missing checklist and the Diligence Readiness Score.

No I/O — the repository supplies the set of present doc-type ids, this module
shapes the user-facing structure, and the router serialises it. Keeping it pure
means the (fiddly) score arithmetic is unit-tested in isolation.

Readiness score (design doc, "Checklist + score"):
* Driven by the 10 user-uploadable minimum items (``MINIMUM_SET`` where
  ``uploadable``). Each present = ``100 / 10 = 10`` points, capped at 100, so a
  fully diligent v1 user reaches a true 100 ("8 of 10 must-haves = 80").
* Items 10 & 11 (Advocate Legal Opinion, Court Case Search Report) are Phase-2
  outputs — advisory rows that never subtract from the score.
* Non-minimum docs present show as "extra" (green) but add nothing.
"""
from __future__ import annotations

from api.dossier import taxonomy as tax


def readiness_score(present: set[str]) -> dict:
    """Compute the 0–100 readiness score from present doc-type ids.

    Only the uploadable minimum items count. An item satisfied by *any* of its
    doc types counts once. Returns the score plus the have/total breakdown so
    the UI can render "8 of 10 must-haves".
    """
    have = 0
    for item in tax.MINIMUM_SET:
        if not item.uploadable:
            continue
        if any(dt in present for dt in item.doc_type_ids):
            have += 1
    total = tax.SCORABLE_ITEM_COUNT
    points_each = 100 / total if total else 0
    score = min(100, round(have * points_each))
    return {"score": score, "have": have, "total": total}


def _minimum_item_view(item: tax.MinimumItem, present: set[str]) -> dict:
    is_present = any(dt in present for dt in item.doc_type_ids)
    view: dict = {
        "label": item.label,
        "doc_type_ids": list(item.doc_type_ids),
        "uploadable": item.uploadable,
        "present": is_present,
        # Advisory = a minimum item the user can't upload yet (Phase-2 output).
        # It shows "recommended — here's how to get one" and never subtracts.
        "advisory": not item.uploadable,
    }
    if not is_present:
        link = tax.portal_link_for(item.doc_type_ids[0])
        if link is not None:
            view["go_get_it"] = {"portal": link.portal, "url": link.url, "how": link.how}
    return view


def build_checklist(present: set[str]) -> dict:
    """Full structured checklist for a dossier.

    ``present`` is the set of known doc-type ids the dossier currently holds
    (already filtered to checklist-relevant ids by the repository).

    Returns:
      * ``score`` — the readiness block from :func:`readiness_score`
      * ``minimum_set`` — the 12 must-haves, each present/missing with a
        "go get it" link when missing; uploadable items first, then advisory
      * ``missing_minimum`` — riskiest-gap-first list of missing uploadable
        must-haves (what the UI leads with — Premise 2's "what's missing" alert)
      * ``categories`` — the full 9-category taxonomy with every doc type marked
        present / missing, plus per-category counts
    """
    present = set(present)

    minimum_views = [_minimum_item_view(m, present) for m in tax.MINIMUM_SET]
    # Stable, useful ordering: uploadable-and-missing first (the actionable
    # gaps), then uploadable-and-present, then advisory rows last.
    def _min_sort_key(v: dict) -> tuple[int, int]:
        if v["advisory"]:
            return (2, 0)
        return (0 if not v["present"] else 1, 0)
    minimum_views.sort(key=_min_sort_key)

    missing_minimum = [
        v for v in minimum_views
        if v["uploadable"] and not v["present"]
    ]

    categories: list[dict] = []
    for cat in tax.CATEGORIES:
        items = []
        have = 0
        for d in tax.DOC_TYPES:
            if d.category != cat.id:
                continue
            is_present = d.id in present
            have += 1 if is_present else 0
            items.append({
                "doc_type": d.id,
                "label": d.label,
                "conditional": d.conditional,
                "present": is_present,
            })
        categories.append({
            "id": cat.id,
            "label": cat.label,
            "have": have,
            "total": len(items),
            "doc_types": items,
        })

    return {
        "score": readiness_score(present),
        "minimum_set": minimum_views,
        "missing_minimum": missing_minimum,
        "categories": categories,
    }
