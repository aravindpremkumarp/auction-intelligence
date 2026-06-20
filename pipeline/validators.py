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
import re

# Penalty (0-100 scale) per severity; score = 100 - sum(penalties), floored at 0.
_PENALTY = {"high": 25, "med": 10, "low": 4}

# Plausible single-property rupee reserve price: Rs 10k .. Rs 100 crore.
_RESERVE_MIN, _RESERVE_MAX = 10_000, 10_000_000_000
# EMD is conventionally ~10% of reserve; flag well outside this band.
_EMD_LO, _EMD_HI = 0.04, 0.25
_LEGAL = {"SARFAESI", "DRT", "IBC", "other"}
# High-value fields whose per-batch coverage % is the main improvement signal.
COVERAGE_FIELDS = (
    "legal_basis", "bank_name", "possession_type", "reserve_price_num", "emd_num",
    "village", "taluk", "district", "registration_district",
    "registration_sub_district", "borrower", "location", "extent", "identifier",
)
_LOT_MARKER = re.compile(r"\b(S\.?\s?No|Sr\.?\s?No|Sl\.?\s?No|Item\s*No)\b", re.I)


def _num(v):
    try:
        return float(re.sub(r"[^\d.]", "", str(v)))
    except (TypeError, ValueError):
        return None


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
        if c == "secured_creditor":
            sec = a
        elif c == "auction_terms":
            r, m = _num(a.get("reserve_price_num")), _num(a.get("emd_num"))
            if r is not None:
                reserves[li] = r
            if m is not None:
                emds[li] = m

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

    # ── multi-lot recall heuristic ───────────────────────────────────────────
    if source_text:
        markers = len(_LOT_MARKER.findall(source_text))
        # crude: many lot markers but few distinct lots extracted -> under-recall
        if markers >= 3 and len(lots) * 2 < markers:
            flag("lot_under_recall", "med",
                 f"{markers} lot markers in source but only {len(lots)} lot(s) extracted")

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
        },
    }
