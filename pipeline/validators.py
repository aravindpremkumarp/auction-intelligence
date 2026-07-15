"""Label-free quality validators for LangExtract auction-notice output.

These checks need NO ground truth — they use the grounding + structural/format
consistency of one notice's extractions to flag likely problems. They are the
signal that drives the incremental-improvement loop: aggregate the issue codes
across a 30-doc batch (see pipeline/extract_batch.py) to see which failure
patterns recur, then fix the prompt/examples and re-gate on evals/.

Usage:
    from pipeline.validators import validate
    report = validate(result.extractions, source_text=markdown)
    # report -> {"score": int, "issues": [{code,severity,msg}], "fields": {...}, "stats": {...}}
"""
from __future__ import annotations

import collections
import json
import re
from pathlib import Path

# Penalty (0-100 scale) per severity; score = 100 - sum(penalties), floored at 0.
_PENALTY = {"high": 25, "med": 10, "low": 4}

# ── identifier-kind normalization ────────────────────────────────────────────
# The prompt instructs an exact enum for identifier `kind`, but models sometimes
# copy the document's label ("T.S.No", "Sy No", "Block No") instead. This maps
# such drift back to the canonical enum; unmappable kinds are flagged
# kind_invalid below. Shared by pipeline/load_extractions.py and the eval.
_KINDS_PATH = Path(__file__).resolve().parent / "lookups" / "identifier_kinds.json"
_KINDS = json.loads(_KINDS_PATH.read_text(encoding="utf-8"))
CANONICAL_KINDS = frozenset(_KINDS["canonical"])
_KIND_ALIASES = _KINDS["aliases"]


def normalize_identifier_kind(kind):
    """Return (canonical_kind_or_original, changed).

    "T.S.No" -> ("survey_old", True); "survey_old" -> ("survey_old", False);
    "shop" (no mapping) -> ("shop", False) — caller may flag kind_invalid.
    """
    if not kind or kind in CANONICAL_KINDS:
        return kind, False
    squashed = re.sub(r"[^a-z0-9]", "", str(kind).lower())
    mapped = _KIND_ALIASES.get(squashed)
    if mapped:
        return mapped, True
    return kind, False

# Plausible single-property rupee reserve price: Rs 10k .. Rs 100 crore.
_RESERVE_MIN, _RESERVE_MAX = 10_000, 10_000_000_000
# EMD is conventionally ~10% of reserve; flag well outside this band.
_EMD_LO, _EMD_HI = 0.04, 0.25
_LEGAL = {"SARFAESI", "DRT", "IBC", "other"}
# High-value fields whose per-batch coverage % is the main improvement signal.
# Mixed convention (kept): entity-class names (borrower/location/boundary/...),
# attr names (village/possession_type/...), and identifier kinds (flat/floor/
# block — added to present_fields in validate()).
COVERAGE_FIELDS = (
    "legal_basis", "bank_name", "possession_type", "reserve_price_num", "emd_num",
    "village", "taluk", "district", "registration_district",
    "registration_sub_district", "borrower", "location", "extent", "identifier",
    "boundary", "full_description", "full_terms", "extras",
    "flat", "floor", "block", "measurement", "undivided_share",
    "address", "encumbrance", "hobli",
)
_LOT_MARKER = re.compile(r"\b(S\.?\s?No|Sr\.?\s?No|Sl\.?\s?No|Item\s*No)\b", re.I)

# Classes that make up a lot's property DESCRIPTION — every one of these must fall
# INSIDE that lot's full_description span (full_description is their verbatim
# union / source of truth). Parties, terms, and notice-level classes are excluded.
_DESCRIPTION_CLASSES = frozenset(
    {"property", "location", "extent", "boundary", "identifier", "schedule"})


def _num(v):
    try:
        return float(re.sub(r"[^\d.]", "", str(v)))
    except (TypeError, ValueError):
        return None


def _span_of(e):
    """(start, end) char span of an extraction, or None when ungrounded.

    Reads char_interval.start_pos/end_pos — present on live LangExtract results
    and on the shim pipeline/extract_batch builds from stored extraction_json."""
    ci = getattr(e, "char_interval", None)
    if ci is None:
        return None
    s, t = getattr(ci, "start_pos", None), getattr(ci, "end_pos", None)
    if s is None or t is None:
        return None
    return int(s), int(t)


def _norm_ws(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def full_description_coverage(extractions) -> dict:
    """Per-lot check that full_description is the complete source of truth.

    Design rule: full_description is the verbatim union of a lot's descriptive
    detail, so every survey/identifier, village/taluk/district, extent and
    boundary must be DERIVABLE from it. A descriptive entity counts as covered
    when its char span sits INSIDE that lot's full_description span OR its text
    appears within the full_description text (the text arm forgives a value that
    is merely repeated at a second position outside the block — still derivable).
    An entity covered by neither means full_description was truncated before that
    detail, so the field can no longer be derived from it.

    Returns per-notice aggregates: lots that have descriptive spans but no
    full_description, and lots with detail falling outside it (offending classes).
    Entities with neither a span nor text are skipped (nothing to check).
    """
    fd_by_lot: dict = {}          # lot -> {"span": (s,e)|None, "text": str}
    gran_by_lot: dict = {}        # lot -> [(span|None, text, cls), ...]
    for e in extractions:
        c = getattr(e, "extraction_class", None)
        li = (getattr(e, "attributes", None) or {}).get("lot_index") or "1"
        sp = _span_of(e)
        txt = _norm_ws(getattr(e, "extraction_text", ""))
        if c == "full_description":
            slot = fd_by_lot.setdefault(li, {"span": None, "text": ""})
            if sp:
                slot["span"] = ((min(slot["span"][0], sp[0]), max(slot["span"][1], sp[1]))
                                if slot["span"] else sp)
            if txt:
                slot["text"] = (slot["text"] + " " + txt).strip()
        elif c in _DESCRIPTION_CLASSES and (sp or txt):
            gran_by_lot.setdefault(li, []).append((sp, txt, c))

    missing_fd, incomplete = [], {}
    for li, spans in gran_by_lot.items():
        fd = fd_by_lot.get(li)
        if fd is None or (fd["span"] is None and not fd["text"]):
            missing_fd.append(li)
            continue
        outside = set()
        for sp, txt, cls in spans:
            by_span = (sp and fd["span"] and fd["span"][0] <= sp[0] <= sp[1] <= fd["span"][1])
            by_text = (txt and fd["text"] and txt in fd["text"])
            if not (by_span or by_text):
                outside.add(cls)
        if outside:
            incomplete[li] = sorted(outside)
    return {
        "lots_with_description": len(gran_by_lot),
        "lots_missing_full_description": sorted(missing_fd),
        "lots_incomplete": incomplete,
    }


def validate(extractions, source_text: str = "") -> dict:
    issues: list[dict] = []

    def flag(code, severity, msg):
        issues.append({"code": code, "severity": severity, "msg": msg})

    classes = collections.Counter()
    lots: set = set()
    reserves: dict = {}   # lot_index -> reserve
    emds: dict = {}       # lot_index -> emd
    present_fields: set = set()
    sec: dict = {}
    ungrounded = nullvals = 0
    invalid_kinds: set = set()
    uds_parent: dict = {}   # lot_index -> {parent-extent nums}
    own_area: dict = {}     # lot_index -> {total_area/extent_sqft nums}

    for e in extractions:
        a = e.attributes or {}
        c = e.extraction_class
        classes[c] += 1
        present_fields.add(c)             # class presence (borrower/location/...)
        li = a.get("lot_index") or "1"
        lots.add(li)
        if getattr(e, "char_interval", None) is None:
            ungrounded += 1
        for k, v in a.items():
            if k != "lot_index" and v not in (None,):
                present_fields.add(k)     # attribute presence (village/...)
            if isinstance(v, str) and v.strip().lower() in {"null", "na", "n/a", ""}:
                nullvals += 1
        if c == "identifier" and a.get("kind"):
            kind, _ = normalize_identifier_kind(a["kind"])
            present_fields.add(kind)      # kind presence (flat/floor/block/...)
            if kind not in CANONICAL_KINDS:
                invalid_kinds.add(str(a["kind"]))
        if c == "secured_creditor":
            # A multi-branch/multi-lot notice repeats secured_creditor; the
            # first entity carries legal_basis etc. — merge first-non-null
            # instead of letting the last (usually sparse) one win.
            for k, v in a.items():
                sec.setdefault(k, v)
        elif c == "auction_terms":
            r, m = _num(a.get("reserve_price_num")), _num(a.get("emd_num"))
            if r is not None:
                reserves[li] = r
            if m is not None:
                emds[li] = m
        elif c == "extent":
            p = _num(a.get("uds_parent_extent"))
            if p is not None:
                uds_parent.setdefault(li, set()).add(round(p, 2))
            for k in ("total_area", "extent_sqft"):
                v = _num(a.get(k))
                if v is not None:
                    own_area.setdefault(li, set()).add(round(v, 2))

    # ── core completeness ────────────────────────────────────────────────────
    if not classes.get("secured_creditor"):
        flag("missing_secured_creditor", "high", "no secured_creditor entity")
    if not classes.get("borrower"):
        flag("missing_borrower", "high", "no borrower entity")
    if not classes.get("location"):
        flag("missing_location", "med", "no location entity")
    if not reserves:
        flag("missing_reserve_price", "high", "no reserve_price_num in any auction_terms")
    if not classes.get("extent"):
        flag("missing_extent", "low", "no extent entity")

    # ── grounding / cleanliness ──────────────────────────────────────────────
    if ungrounded:
        flag("ungrounded", "med", f"{ungrounded} extraction(s) not grounded to source")
    if nullvals:
        flag("null_value", "low", f"{nullvals} literal 'null'/empty attribute value(s)")
    if invalid_kinds:
        flag("kind_invalid", "low",
             f"identifier kind(s) outside the enum: {sorted(invalid_kinds)}")
    if classes.get("extras", 0) > 5:
        flag("extras_excess", "low",
             f"{classes['extras']} extras entities (prompt caps at ~5)")

    # ── field sanity ─────────────────────────────────────────────────────────
    lb = sec.get("legal_basis")
    if lb not in _LEGAL:
        flag("legal_basis_bad", "low", f"legal_basis={lb!r} missing/invalid")
    for li, r in reserves.items():
        if not (_RESERVE_MIN <= r <= _RESERVE_MAX):
            flag("reserve_out_of_range", "med", f"lot {li} reserve={r:.0f} implausible")
    for li, r in reserves.items():
        m = emds.get(li)
        if m and r and not (_EMD_LO <= m / r <= _EMD_HI):
            flag("emd_ratio_off", "low",
                 f"lot {li} emd/reserve={m / r:.2f} (expect ~0.10)")
    # A flat's UDS parent-plot extent must live ONLY in uds_parent_extent — never
    # be echoed as the property's own area. Overlap means the whole plot got
    # recorded as the flat's size (e.g. a 760 sq.ft flat shown as 2257 sq.ft).
    for li, parents in uds_parent.items():
        overlap = parents & own_area.get(li, set())
        if overlap:
            flag("uds_parent_as_own_area", "med",
                 f"lot {li}: UDS parent extent {sorted(overlap)} also recorded as "
                 f"the property's own area (total_area/extent_sqft) — for a flat "
                 f"that value belongs only in uds_parent_extent")

    # ── multi-lot recall heuristic ───────────────────────────────────────────
    if source_text:
        markers = len(_LOT_MARKER.findall(source_text))
        # crude: many lot markers but few distinct lots extracted -> under-recall
        if markers >= 3 and len(lots) * 2 < markers:
            flag("lot_under_recall", "med",
                 f"{markers} lot markers in source but only {len(lots)} lot(s) extracted")

    # ── full_description completeness (source-of-truth invariant) ─────────────
    cov = full_description_coverage(extractions)
    if cov["lots_missing_full_description"]:
        flag("missing_full_description", "med",
             f"lot(s) {cov['lots_missing_full_description']} have property details "
             f"but no full_description block")
    if cov["lots_incomplete"]:
        detail = "; ".join(f"lot {li}: {cls}" for li, cls in cov["lots_incomplete"].items())
        flag("full_description_incomplete", "med",
             f"full_description does not cover all descriptive spans ({detail})")

    score = max(0, 100 - sum(_PENALTY[i["severity"]] for i in issues))
    return {
        "score": score,
        "issues": issues,
        "fields": sorted(present_fields),
        "stats": {
            "n_extractions": sum(classes.values()),
            "classes": dict(classes),
            "lots": len(lots),
            "ungrounded": ungrounded,
            "null_values": nullvals,
            "full_description_incomplete_lots": len(cov["lots_incomplete"]),
            "lots_missing_full_description": len(cov["lots_missing_full_description"]),
        },
    }
