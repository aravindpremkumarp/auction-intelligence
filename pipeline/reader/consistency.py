"""Cross-field rules — the checks grounding alone cannot make.

A value can be perfectly grounded and still be wrong: an EMD larger than the
reserve price, an auction that ends before it starts, a UDS larger than the
plot it is carved from, an extent that contradicts the area written inside
the description. These rules run on the converted entities, cost no model
call, and mark the offending field ``CONTESTED`` with the rule's name, which
is what sends it to the targeted re-read (pipeline/reader/stability.py) and,
failing that, to the top of the review queue. Thresholds start at the
validator's own (pipeline/validators: EMD 4%–25% of reserve) and are tuned
against gold, never widened by hand.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from pipeline.measures import parse_area
from pipeline.validators import _EMD_HI, _EMD_LO

CONTESTED = "CONTESTED"
YEARS_AROUND_NOTICE = 2
#: An area in the description that disagrees with the extent by more than
#: this share is a contradiction, not rounding.
EXTENT_TOLERANCE = 0.05
_AREA_IN_TEXT = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:¼|½|¾)?\s*(?:sq\.?\s*(?:ft|feet|m|mt|mtr|meter|metre|yd|yard)s?|"
    r"square\s*(?:feet|metres|meters|yards)|cents?|acres?|ares?|hectares?|guntha|grounds?)",
    re.I)


@dataclass(frozen=True)
class Finding:
    lot: str
    field: str          # attr or class the finding is about
    rule: str
    message: str


def _lot(e: dict) -> str:
    return str((e.get("attrs") or {}).get("lot_index") or "1")


def _num(v):
    try:
        return float(str(v).replace(",", "")) if v not in (None, "") else None
    except ValueError:
        return None


def _iso_date(v) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _mark(e: dict, field: str, rule: str) -> None:
    a = e.setdefault("attrs", {})
    a["evidence"] = CONTESTED
    rules = [r for r in str(a.get("rule") or "").split(";") if r]
    tag = f"{field}:{rule}"
    if tag not in rules:
        rules.append(tag)
    a["rule"] = ";".join(rules)


def check(entities: list[dict], *, notice_date: str | None = None,
          segments: dict[str, tuple[int, int]] | None = None) -> list[Finding]:
    """Apply every rule; mark offending entities in place; return findings.

    ``segments`` is {lot_index: (start, end)}: when given, every grounded
    lot-level entity must lie inside its own lot's span (the cross-lot
    contamination rule, enforced here as well as by the grounding window).
    """
    out: list[Finding] = []
    by_lot: dict[str, list[dict]] = {}
    for e in entities:
        by_lot.setdefault(_lot(e), []).append(e)
    nd = _iso_date(notice_date)

    for lot, ents in by_lot.items():
        terms = [e for e in ents if e.get("cls") == "auction_terms"]
        props = [e for e in ents if e.get("cls") == "property"]
        extents = [e for e in ents if e.get("cls") == "extent"]
        fds = [e for e in ents if e.get("cls") == "full_description"]

        for t in terms:
            a = t.get("attrs") or {}
            r, m = _num(a.get("reserve_price_num")), _num(a.get("emd_num"))
            if r and m:
                if m > r:
                    _mark(t, "emd_num", "emd_exceeds_reserve")
                    out.append(Finding(lot, "emd_num", "emd_exceeds_reserve",
                                       f"lot {lot}: EMD {m:.0f} > reserve {r:.0f}"))
                elif not (_EMD_LO <= m / r <= _EMD_HI):
                    _mark(t, "emd_num", "emd_ratio_off")
                    out.append(Finding(lot, "emd_num", "emd_ratio_off",
                                       f"lot {lot}: EMD/reserve = {m / r:.2f}"))
            s, e_, d = (_iso_date(a.get("auction_start_dt")), _iso_date(a.get("auction_end_dt")),
                        _iso_date(a.get("application_deadline_dt")))
            if s and e_ and e_ < s:
                _mark(t, "auction_end_dt", "end_before_start")
                out.append(Finding(lot, "auction_end_dt", "end_before_start",
                                   f"lot {lot}: auction ends {e_} before it starts {s}"))
            if s and d and d > s:
                _mark(t, "application_deadline_dt", "deadline_after_start")
                out.append(Finding(lot, "application_deadline_dt", "deadline_after_start",
                                   f"lot {lot}: deadline {d} after auction start {s}"))
            for key in ("auction_start_dt", "auction_end_dt", "application_deadline_dt",
                        "inspection_dt"):
                v = _iso_date(a.get(key))
                if v and nd and abs(v.year - nd.year) > YEARS_AROUND_NOTICE:
                    _mark(t, key, "date_far_from_notice")
                    out.append(Finding(lot, key, "date_far_from_notice",
                                       f"lot {lot}: {key} {v} is far from the notice date {nd}"))

        for p in props:
            pd = _iso_date((p.get("attrs") or {}).get("possession_date"))
            if pd and nd and pd > nd:
                _mark(p, "possession_date", "possession_after_notice")
                out.append(Finding(lot, "possession_date", "possession_after_notice",
                                   f"lot {lot}: possession {pd} after the notice date {nd}"))

        uds = parent = None
        for x in extents:
            a = x.get("attrs") or {}
            if a.get("undivided_share"):
                uds = parse_area(str(a["undivided_share"]))[2]
            if a.get("uds_parent_extent"):
                parent = parse_area(str(a["uds_parent_extent"]))[2]
        if uds and parent and uds > parent:
            for x in extents:
                if (x.get("attrs") or {}).get("undivided_share"):
                    _mark(x, "undivided_share", "uds_exceeds_parent")
            out.append(Finding(lot, "undivided_share", "uds_exceeds_parent",
                               f"lot {lot}: UDS {uds:.0f} sq.ft > parent {parent:.0f} sq.ft"))

        # The extent must agree with an area the description itself states.
        fd_text = " ".join(e.get("text") or "" for e in fds)
        stated = [parse_area(m.group(0))[2] for m in _AREA_IN_TEXT.finditer(fd_text)]
        stated = [v for v in stated if v]
        for x in extents:
            a = x.get("attrs") or {}
            v = _num(a.get("extent_sqft"))
            if v and stated and not any(abs(v - s) <= EXTENT_TOLERANCE * max(v, s) for s in stated):
                _mark(x, "extent_sqft", "extent_contradicts_description")
                out.append(Finding(lot, "extent_sqft", "extent_contradicts_description",
                                   f"lot {lot}: extent {v:.0f} sq.ft matches none of "
                                   f"{[round(s) for s in stated]} in the description"))

        if segments and lot in segments:
            lo, hi = segments[lot]
            for e in ents:
                if e.get("start") is None or e.get("cls") in ("secured_creditor", "contact",
                                                              "emd_account", "full_terms"):
                    continue
                if not (lo <= e["start"] and e["end"] <= hi):
                    _mark(e, e.get("cls", "?"), "outside_own_lot")
                    out.append(Finding(lot, e.get("cls", "?"), "outside_own_lot",
                                       f"lot {lot}: {e.get('cls')} span {e['start']}-{e['end']} "
                                       f"lies outside the lot {lo}-{hi}"))
    return out


__all__ = ["check", "Finding", "CONTESTED", "EXTENT_TOLERANCE", "YEARS_AROUND_NOTICE"]
