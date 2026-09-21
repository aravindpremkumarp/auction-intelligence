"""Per-lot key-entity checklist for the extraction review stage.

A sale notice is only usable once every lot carries a small set of facts:
reserve price, auction date, property type, location, extent, the full
description block and possession. The review stage exists to make sure those
seven are present for every lot — not to re-read forty spans. This module turns
one document's stored extraction (plus the reviewer's corrections) into that
checklist, and into the key score the queue sorts by.

Three states per cell:

* ``filled``  — an entity (or attribute) carries the value.
* ``missing`` — nothing does. This is what the reviewer has to act on: fill it
  from the notice text, or mark it absent.
* ``absent``  — the reviewer says the notice never states it. Counted as done,
  because a value that is not in the notice is not an extraction miss. This is
  what keeps possession honest: many notices list "Constructive/Symbolic/
  Physical" without choosing, and pipeline/validators.py deliberately does not
  penalise a blank there either.

Reviewer input rides in ``Document.extraction_corrections_json`` beside the
per-field text corrections, under two prefixed keys:

    "add:<id>"          {cls, text, start, end, attrs, by, at}
                        an entity the model missed, added by the reviewer;
                        pipeline.apply_extractions.entities_with_corrections
                        appends it so promotion and the gold export see it.
    "absent:<lot>:<key>" {by, at}
                        "not in the notice" for one lot's key.

Lots come from ``lot_index`` like everywhere else (an entity without one is
lot "1"). When the classification gate recorded ``expected_lot_count`` and the
extraction has fewer lots, the un-extracted lots appear with every cell
missing — a lot the model dropped is the biggest miss of all, and a checklist
that only counted the lots it was given would never show it.
"""
from __future__ import annotations

import json
import re

from pipeline.apply_extractions import added_entities, parse_money

# (key, label, entity class, attribute). attribute=None means the entity's own
# text is the value.
KEY_ENTITIES: tuple[tuple[str, str, str, str | None], ...] = (
    ("reserve_price",    "Reserve price",    "auction_terms",    "reserve_price_num"),
    ("auction_date",     "Auction date",     "auction_terms",    "auction_start_dt"),
    ("property_type",    "Property type",    "property",         "property_type"),
    ("location",         "Location",         "location",         None),
    ("extent",           "Extent",           "extent",           None),
    ("full_description", "Full description", "full_description", None),
    ("possession_type",  "Possession",       "property",         "possession_type"),
)
KEYS = tuple(k for k, _, _, _ in KEY_ENTITIES)
KEY_LABELS = {k: label for k, label, _, _ in KEY_ENTITIES}
KEY_CLASS = {k: cls for k, _, cls, _ in KEY_ENTITIES}
KEY_ATTR = {k: attr for k, _, _, attr in KEY_ENTITIES}

# Notice-level classes: never a lot's own, so they neither create a lot nor
# fill a cell.
_NOTICE_LEVEL = frozenset({"secured_creditor", "contact", "emd_account",
                           "full_terms", "extras"})

_ABSENT_RE = re.compile(r"^absent:(?P<lot>[^:]+):(?P<key>[a-z_]+)$")


def absent_key(lot_index: str, key: str) -> str:
    return f"absent:{lot_index}:{key}"


def absent_marks(corrections: dict) -> set[tuple[str, str]]:
    """{(lot_index, key)} the reviewer marked as not in the notice."""
    out: set[tuple[str, str]] = set()
    if not isinstance(corrections, dict):
        return out
    for k, v in corrections.items():
        m = _ABSENT_RE.match(str(k))
        if m and isinstance(v, dict) and m.group("key") in KEYS:
            out.add((m.group("lot"), m.group("key")))
    return out


def _loads(s, default):
    try:
        v = json.loads(s or "")
    except (json.JSONDecodeError, TypeError):
        return default
    return v if isinstance(v, type(default)) else default


def _lot_of(e: dict) -> str:
    attrs = e.get("attrs") or {}
    return str(attrs.get("lot_index") or "1")


def _snippet(t: str, n: int = 80) -> str:
    t = re.sub(r"<[^>]*>", " ", str(t or ""))
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= n else t[:n] + "…"


def _fmt_money(v) -> str | None:
    n = parse_money(v)
    if n is None:
        return None
    # Indian grouping: 12,50,000
    s = str(n)
    if len(s) <= 3:
        return "₹" + s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return "₹" + ",".join(parts) + "," + tail


def _location_value(e: dict) -> str:
    a = e.get("attrs") or {}
    parts = [str(a[k]).strip() for k in ("village", "taluk", "district")
             if a.get(k) and str(a[k]).strip().lower() not in ("null", "none")]
    return ", ".join(parts) if parts else _snippet(e.get("text"))


def _extent_value(e: dict) -> str:
    a = e.get("attrs") or {}
    for k in ("total_area", "extent_sqft", "built_up_area",
              "super_built_up_area", "carpet_area", "undivided_share"):
        v = a.get(k)
        if v not in (None, "") and str(v).strip().lower() not in ("null", "none"):
            return f"{v} sq.ft" if k == "extent_sqft" else str(v)
    return _snippet(e.get("text"))


def _has(v) -> bool:
    return v not in (None, "") and str(v).strip().lower() not in ("null", "none", "na", "n/a")


def key_checklist(entities: list[dict], corrections: dict | None = None,
                  expected_lot_count: int | None = None) -> dict:
    """The lot × key table for one document.

    ``entities`` is the stored entity list with reviewer corrections already
    applied (pipeline.apply_extractions.entities_with_corrections — which also
    appends reviewer-added entities); ``corrections`` is the raw corrections
    dict, read here only for the absent marks.

    Returns::

        {"lots": [{"lot_index": "1", "extracted": True,
                   "cells": {key: {"status": "filled"|"missing"|"absent",
                                   "value": str|None, "field_id": str|None,
                                   "inherited": bool}}}],
         "total": 14, "filled": 11, "absent": 1, "missing": 2, "score": 86,
         "missing_labels": ["lot 2: extent", "lot 2: possession"]}

    score = (filled + absent) / total, 0–100. An empty extraction with no
    expected lot count still describes one lot, all missing, so it scores 0
    rather than vanishing from the queue.
    """
    absent = absent_marks(corrections or {})
    by_lot: dict[str, list[dict]] = {}
    for e in entities:
        if not isinstance(e, dict) or e.get("cls") in _NOTICE_LEVEL:
            continue
        by_lot.setdefault(_lot_of(e), []).append(e)

    # Notice-level fallback for the auction date: one date is normally stated
    # once for every lot, and the model tags it on whichever lot it saw first.
    doc_date: tuple[str, str] | None = None
    for e in entities:
        if isinstance(e, dict) and e.get("cls") == "auction_terms":
            v = (e.get("attrs") or {}).get("auction_start_dt")
            if _has(v):
                doc_date = (str(v), str(e.get("id") or ""))
                break

    def _sort_key(li: str):
        return (0, int(li)) if li.isdigit() else (1, li)

    lot_ids = sorted(by_lot, key=_sort_key)
    if not lot_ids:
        lot_ids = ["1"]
    n_expected = int(expected_lot_count) if expected_lot_count else 0
    extracted = set(lot_ids)
    # Lots the reviewer counted but the model never emitted.
    nxt = 1
    while len(lot_ids) < n_expected:
        while str(nxt) in extracted:
            nxt += 1
        lot_ids.append(str(nxt))
        nxt += 1

    lots_out = []
    filled = n_absent = 0
    missing_labels: list[str] = []
    for li in lot_ids:
        ents = by_lot.get(li, [])
        cells: dict[str, dict] = {}
        for key, label, cls, attr in KEY_ENTITIES:
            cell = {"status": "missing", "value": None, "field_id": None,
                    "inherited": False}
            for e in ents:
                if e.get("cls") != cls:
                    continue
                fid = str(e.get("id") or "")
                if attr is None:
                    if not _has(e.get("text")):
                        continue
                    if key == "location":
                        val = _location_value(e)
                    elif key == "extent":
                        val = _extent_value(e)
                    else:
                        val = _snippet(e.get("text"))
                    cell.update(status="filled", value=val, field_id=fid)
                    break
                v = (e.get("attrs") or {}).get(attr)
                if not _has(v):
                    continue
                val = _fmt_money(v) if key == "reserve_price" else str(v)
                if val is None:
                    continue
                cell.update(status="filled", value=val, field_id=fid)
                break
            # A lot the model never emitted inherits nothing: its row is
            # meant to read as the whole lot missing, not as one date short.
            if (cell["status"] != "filled" and key == "auction_date"
                    and doc_date and li in extracted):
                cell.update(status="filled", value=doc_date[0],
                            field_id=doc_date[1] or None, inherited=True)
            if cell["status"] != "filled" and (li, key) in absent:
                cell["status"] = "absent"
            if cell["status"] == "filled":
                filled += 1
            elif cell["status"] == "absent":
                n_absent += 1
            else:
                missing_labels.append(f"lot {li}: {label.lower()}")
            cells[key] = cell
        lots_out.append({"lot_index": li, "extracted": li in extracted,
                         "cells": cells})

    total = len(lot_ids) * len(KEY_ENTITIES)
    done = filled + n_absent
    return {
        "lots": lots_out,
        "total": total,
        "filled": filled,
        "absent": n_absent,
        "missing": total - done,
        "score": int(round(100 * done / total)) if total else 0,
        "missing_labels": missing_labels,
    }


def key_checklist_from_stored(extraction_json: str | None,
                              corrections_json: str | None,
                              expected_lot_count: int | None = None) -> dict:
    """key_checklist() straight from the two Document properties."""
    from pipeline.apply_extractions import entities_with_corrections
    ents = entities_with_corrections(extraction_json or "[]", corrections_json)
    corr = _loads(corrections_json, {})
    return key_checklist(ents, corr, expected_lot_count)


def stamp_key_scores(filenames: list[str], chunk: int = 200) -> int:
    """Recompute and store ``extraction_key_score`` / ``extraction_key_missing``
    for these documents. One function for every writer — the loader, the
    single-document rerun, the reset script, the backfill and each reviewer
    edit — so the stored number the queue sorts on can only ever mean one
    thing. Reads the three inputs back from the graph rather than taking them
    as arguments, because no writer holds all three (the loader has the
    entities but not the corrections it preserved). Returns rows written."""
    from api.neo4j_client import run_query, run_read_query
    written = 0
    for i in range(0, len(filenames), chunk):
        names = filenames[i:i + chunk]
        rows = run_read_query(
            """
            UNWIND $fns AS fn
            MATCH (d:Document {filename: fn})
            WHERE d.extraction_json IS NOT NULL
            RETURN d.filename AS filename,
                   d.extraction_json AS ej,
                   coalesce(d.extraction_corrections_json, '{}') AS cj,
                   coalesce(d.stitched_expected_lot_count, d.expected_lot_count) AS elc
            """,
            {"fns": names}, max_rows=chunk)
        out = []
        for r in rows:
            elc = r.get("elc")
            k = key_checklist_from_stored(r.get("ej"), r.get("cj"),
                                          int(elc) if elc is not None else None)
            out.append({"filename": r["filename"], "score": k["score"],
                        "missing": k["missing"]})
        if not out:
            continue
        run_query(
            """
            UNWIND $rows AS row
            MATCH (d:Document {filename: row.filename})
            SET d.extraction_key_score   = row.score,
                d.extraction_key_missing = row.missing
            """,
            {"rows": out})
        written += len(out)
    return written


__all__ = ["KEY_ENTITIES", "KEYS", "KEY_LABELS", "KEY_CLASS", "KEY_ATTR",
           "absent_key", "absent_marks", "added_entities",
           "key_checklist", "key_checklist_from_stored", "stamp_key_scores"]
