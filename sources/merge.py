"""One merged record per auction event, built from every branch that names it.

Pure: ``merge_event`` takes a cluster — the portal listings the matcher
grouped, the notice lots behind them, their media — and returns the
``:AuctionEvent`` properties ``scripts/build_spine.py`` writes. The spine is
rebuilt from the branches every run, so a better matcher or a corrected
branch is a rerun, never a migration.

Ranking, per field type (spec Decision 5):

    property facts   notice > BAANKNET > bankeauctions > eauctionsindia
    lifecycle        BAANKNET > bankeauctions > notice > eauctionsindia
    price / EMD      portal and notice agree → CONFIRMED; disagree → the
                     notice's figure, flagged for review
    media            any branch; ``has_photos`` is "one image anywhere"

Every chosen value remembers its branch in ``provenance``; every core field
counts toward ``core_complete`` (0–9). Values the branches carry as text
(extent, boundaries) are read with the same extractors the matcher uses.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pipeline.price_agreement import compare_prices
from sources.match import SIDES, extract_boundaries, extract_extent

NOTICE = "notice"
PROPERTY_ORDER = (NOTICE, "baanknet", "bankeauctions", "eauctionsindia")
LIFECYCLE_ORDER = ("baanknet", "bankeauctions", NOTICE, "eauctionsindia")

#: The nine-field property core, in the order docs/SCHEMA.md lists it.
CORE_FIELDS = ("property_type", "district", "extent", "measurement", "possession_type",
               "boundaries", "reserve_price_num", "auction_start_dt", "has_photos")

PROPERTY_FIELDS = ("property_type", "district", "extent_sqft", "extent_kind", "extent_raw",
                   "possession_type", "boundaries", "measurements", "borrower", "encumbrance",
                   "description", "title", "city", "area", "pincode")
LIFECYCLE_FIELDS = ("auction_start_dt", "auction_end_dt", "application_deadline_dt", "auction_status",
                    "inspection_start_dt", "inspection_end_dt", "bid_increment_num", "bank", "url")

_NOT_STATED = {"", "not stated", "unknown", "none", "n/a"}


def _present(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip().lower() not in _NOT_STATED
    if isinstance(v, (list, dict, set, tuple)):
        return len(v) > 0
    return True


# ── branches ─────────────────────────────────────────────────────────────────


def listing_branch(row: dict) -> dict:
    """A portal listing as a branch: ``{"key": "baanknet:bn-1", "source": …,
    "rank": …, "fields": {...}}``. ``row`` is the loader's view of the node
    (``source``, the portal fields, plus the notice-applied ``revenue_district``
    / ``property_type_effective`` / ``boundary_*`` / ``total_area`` which are
    the notice's, not the portal's, and go to the notice branch instead)."""
    source = row.get("source") or "eauctionsindia"
    text = " ".join(t for t in (row.get("title"), row.get("description")) if t)
    bounds = extract_boundaries(text)
    ptype = row.get("portal_property_type") or (row.get("property_types") or [None])[0] or row.get("property_type_raw")
    fields = {
        "property_type": ptype,
        "district": row.get("portal_district"),
        "city": row.get("city"),
        "area": row.get("area"),
        "pincode": row.get("pincode"),
        "extent_raw": row.get("extent_raw") or extract_extent(text),
        "possession_type": row.get("possession_type"),
        "boundaries": bounds if len(bounds) == 4 else None,
        "borrower": row.get("borrower"),
        "borrower_address": row.get("borrower_address"),
        "description": row.get("description"),
        "title": row.get("title"),
        "bank": row.get("bank"),
        "url": row.get("source_url") or row.get("url"),
        "reserve_price_num": row.get("reserve_price_num"),
        "emd_num": row.get("emd_num"),
        "auction_start_dt": row.get("auction_start_dt"),
        "auction_end_dt": row.get("auction_end_dt"),
        "application_deadline_dt": row.get("application_deadline_dt"),
        "auction_status": row.get("auction_status"),
        "inspection_start_dt": row.get("inspection_start_dt"),
        "inspection_end_dt": row.get("inspection_end_dt"),
        "bid_increment_num": row.get("bid_increment_num"),
    }
    return {"key": f"{source}:{row.get('auction_id')}", "source": source,
            "rank": int(row.get("source_rank") or 9), "fields": fields}


def notice_branch(lot: dict, *, applied: dict | None = None) -> dict | None:
    """The sale notice as a branch, from its ``:Lot`` (``lot``) and the values
    ``apply_extractions`` already wrote onto the listing (``applied``:
    ``revenue_district``, ``property_type_effective``, ``boundary_*``,
    ``boundary_measurement_*``, ``total_area``). ``None`` when there is no
    notice evidence at all."""
    applied = applied or {}
    lot = lot or {}
    bounds = {s: v for s, v in (lot.get("boundaries") or {}).items() if v}
    for s in SIDES:
        v = applied.get(f"boundary_{s}")
        if v and s not in bounds:
            bounds[s] = v
    measures = {s: v for s, v in (lot.get("measurements") or {}).items() if v}
    for s in SIDES:
        v = applied.get(f"boundary_measurement_{s}")
        if v and s not in measures:
            measures[s] = v
    fields = {
        "property_type": lot.get("property_type") or applied.get("property_type_effective"),
        "district": lot.get("district") or applied.get("revenue_district"),
        "extent_sqft": lot.get("extent_sqft"),
        "extent_kind": lot.get("extent_kind"),
        "extent_raw": lot.get("extent_raw") or applied.get("total_area"),
        "possession_type": lot.get("possession"),
        "boundaries": bounds if len(bounds) == 4 else None,
        "measurements": measures or None,
        "borrower": lot.get("borrower"),
        "encumbrance": lot.get("encumbrance"),
        "description": lot.get("full_description"),
        "reserve_price_num": lot.get("reserve_price_num"),
        "emd_num": lot.get("emd_num"),
        "auction_start_dt": lot.get("auction_start_dt"),
        "auction_end_dt": lot.get("auction_end_dt"),
        "bid_increment_num": lot.get("bid_increment_num"),
        "attempt_no": lot.get("attempt_no"),
    }
    if not any(_present(v) for v in fields.values()):
        return None
    key = lot.get("lot_key") or applied.get("grounded_source_file") or "applied"
    return {"key": f"{NOTICE}:{key}", "source": NOTICE, "rank": 0, "fields": fields}


# ── picking ──────────────────────────────────────────────────────────────────


def _ordered(branches: list[dict], order: tuple[str, ...]) -> list[dict]:
    pos = {s: i for i, s in enumerate(order)}
    return sorted(branches, key=lambda b: (pos.get(b["source"], len(order)), b["rank"], b["key"]))


def pick(field: str, branches: list[dict], order: tuple[str, ...]) -> tuple[Any, str | None]:
    """The first present value for ``field`` in ranking order, with the
    branch key it came from."""
    for b in _ordered(branches, order):
        v = b["fields"].get(field)
        if _present(v):
            return v, b["key"]
    return None, None


def pick_price(field: str, branches: list[dict]) -> tuple[Any, str | None, str]:
    """Portal and notice agree within tolerance → the portal figure (it is the
    live one) marked ``agree``; disagree → the notice's, marked with the
    verdict for review; one side only → that side, ``unknown``."""
    portal_v, portal_k = pick(field, [b for b in branches if b["source"] != NOTICE], LIFECYCLE_ORDER)
    notice_v, notice_k = pick(field, [b for b in branches if b["source"] == NOTICE], PROPERTY_ORDER)
    verdict, _ = compare_prices(portal_v, notice_v)
    if verdict == "agree":
        return portal_v, portal_k, "agree"
    if verdict == "unknown":
        return (portal_v, portal_k, "unknown") if _present(portal_v) else (notice_v, notice_k, "unknown")
    return notice_v, notice_k, verdict


def event_id_for(listing_ids: list[str]) -> str:
    """Deterministic: the same set of listings always names the same event."""
    ids = sorted(str(i) for i in listing_ids if i)
    if not ids:
        raise ValueError("an event needs at least one listing")
    if len(ids) == 1:
        return f"ev-{ids[0]}"
    return "ev-" + hashlib.sha1("|".join(ids).encode("utf-8")).hexdigest()[:16]


# ── the merge ────────────────────────────────────────────────────────────────


def merge_event(cluster: dict, *, built_at: str | None = None) -> dict:
    """``cluster`` = ``{"listings": [row…], "lots": [lot…], "media": [m…],
    "confidence": "CONFIRMED"|"PROBABLE"|"SINGLE"}`` → the event's properties.

    ``listings`` are the graph's listing rows (``listing_branch`` reads them;
    the notice-applied fields on each also feed ``notice_branch``); ``lots``
    are the notice lots behind them; ``media`` are ``{url, kind, is_main}``.
    """
    listings = cluster.get("listings") or []
    if not listings:
        raise ValueError("a cluster needs at least one listing")
    branches = [listing_branch(r) for r in listings]
    for lot in cluster.get("lots") or []:
        nb = notice_branch(lot)
        if nb:
            branches.append(nb)
    if not any(b["source"] == NOTICE for b in branches):
        for r in listings:                      # notice values applied to the listing, no lot loaded
            nb = notice_branch({}, applied=r)
            if nb:
                branches.append(nb)
                break

    provenance: dict[str, str] = {}
    out: dict[str, Any] = {}
    for f in PROPERTY_FIELDS:
        v, k = pick(f, branches, PROPERTY_ORDER)
        if k:
            out[f], provenance[f] = v, k
    for f in LIFECYCLE_FIELDS:
        v, k = pick(f, branches, LIFECYCLE_ORDER)
        if k:
            out[f], provenance[f] = v, k
    for f in ("reserve_price_num", "emd_num"):
        v, k, verdict = pick_price(f, branches)
        if k:
            out[f], provenance[f] = v, k
        out[f"{f.replace('_num', '')}_agreement"] = verdict

    media = [m for m in (cluster.get("media") or []) if m.get("url")]
    out["has_photos"] = any(m.get("kind") == "image" for m in media)
    out["photo_count"] = sum(1 for m in media if m.get("kind") == "image")
    out["video_count"] = sum(1 for m in media if m.get("kind") == "video")
    if out["has_photos"]:
        provenance["has_photos"] = "media"

    core = {
        "property_type": _present(out.get("property_type")),
        "district": _present(out.get("district")),
        "extent": _present(out.get("extent_sqft")) or _present(out.get("extent_raw")),
        "measurement": _present(out.get("measurements")),
        "possession_type": _present(out.get("possession_type")),
        "boundaries": _present(out.get("boundaries")),
        "reserve_price_num": _present(out.get("reserve_price_num")),
        "auction_start_dt": _present(out.get("auction_start_dt")),
        "has_photos": out["has_photos"],
    }
    out["core_complete"] = sum(core.values())
    out["core_missing"] = [f for f in CORE_FIELDS if not core[f]]

    ids = [str(r.get("auction_id")) for r in listings]
    out["event_id"] = event_id_for(ids)
    out["listing_ids"] = sorted(ids)
    out["sources"] = sorted({b["source"] for b in branches})
    out["confidence"] = cluster.get("confidence") or ("SINGLE" if len(ids) == 1 else "PROBABLE")
    out["provenance"] = provenance
    out["built_at"] = built_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out


def event_node_props(event: dict) -> dict:
    """The event as Neo4j properties: maps become JSON strings (Neo4j has no
    map properties), lists of scalars stay lists, datetimes stay ISO strings
    for the writer to cast."""
    props = {}
    for k, v in event.items():
        if isinstance(v, dict):
            props[f"{k}_json" if not k.endswith("_json") else k] = json.dumps(v, ensure_ascii=False, sort_keys=True)
        else:
            props[k] = v
    return props
