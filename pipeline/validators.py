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
# The priority fields a reviewer weights most — full_description, property_type,
# possession, extent, UDS, borrower, reserve price (the fields that make a lot
# usable and confirm it's a real lot) — carry the top tiers (critical/high).
#
# INVARIANT: one flag per DEFECT KIND, never one per affected lot. The score is
# read as extraction quality, so it must not be a function of how many lots a
# notice happens to contain. Emitting per-lot made a systematic defect multiply:
# with high=20, a quirk recurring across 5 lots alone floored the document at 0.
# A real case — 133 entities across 6 lots with only 6 issues total (a good
# extraction) scored 0, identical to a 1-lot notice missing creditor, borrower,
# location, reserve price AND extent (a broken one). That destroys review triage
# and, worse, the improvement loop: a saturated metric has no gradient, so a
# prompt change can't be told from a regression. Checks that span lots therefore
# collect their lots and flag ONCE, listing them in the message (see
# missing_property_type / possession_type_invalid / missing_uds).
_PENALTY = {"critical": 30, "high": 20, "med": 10, "low": 4}
# Valid committed possession values (Option A: penalise only present-but-invalid;
# a blank possession is often correct — the "Constructive/Symbolic/Physical"
# disjunction has no single answer — so absence is NOT penalised).
_POSSESSION_VALID = {"physical", "symbolic", "constructive"}

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
# A lot marker is a serial-number label followed by a PLAIN small integer —
# "S. No. 1", "Sl.No 2", "Item No. 3" — the numbering of a multi-lot listing.
# It must NOT match a survey-number citation ("S.No.48/30", "Old.S No.48/3A1AM"):
# those carry a subdivision (slash, dash, or letter suffix) and appear in the
# property description of nearly EVERY notice, single-lot ones included, so
# counting them made lot_under_recall fire on notices with nothing missing.
# A bare table header ("<th>S. No.</th>") carries no number and is skipped too.
# The lookbehind drops survey-number families that only differ by a prefix
# letter — "R.S. No. 102" (re-survey), "T.S.No" (town survey), "Old.S No" — which
# carry no subdivision and would otherwise read as a lot number.
_LOT_MARKER = re.compile(
    r"(?<![A-Za-z]\.)\b(?:S|Sr|Sl|Item)\.?\s*No\.?\s*(\d{1,3})\b"
    r"(?!\s*[/-]|[A-Za-z])", re.I)

# Tokens used by the order-insensitive coverage arm below: 3+ chars, lowercased,
# so punctuation and word ORDER stop mattering when checking derivability.
_COVERAGE_TOKEN = re.compile(r"[0-9a-z]+")


def _cov_tokens(s: str) -> list:
    return [t for t in _COVERAGE_TOKEN.findall((s or "").lower()) if len(t) >= 3]

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
    is merely repeated at a second position outside the block — still derivable)
    OR every meaningful token of its text appears somewhere in that text.

    That third (token) arm exists because the extractor often SYNTHESISES an
    entity rather than copying it — emitting a location in canonical order
    ("Village, Taluk, District, State") where the notice reads "District, ...,
    Taluk, ..., Village". Nothing is missing there, but neither the span arm
    (a synthesised entity is ungrounded, so it has no span) nor the substring
    arm can see that, and the lot was flagged incomplete over pure word order.
    Requiring ALL tokens keeps the check's teeth: a genuinely truncated
    description loses whole values, not merely their ordering.

    An entity covered by none of the three means full_description was truncated
    before that detail, so the field can no longer be derived from it.

    Returns per-notice aggregates: lots that have descriptive spans but no
    full_description, and lots with detail falling outside it (offending classes).
    Entities with neither a span nor text are skipped (nothing to check).
    """
    fd_by_lot: dict = {}          # lot -> {"span": (s,e)|None, "text": str}
    gran_by_lot: dict = {}        # lot -> [(span|None, text, cls, attrs), ...]
    for e in extractions:
        c = getattr(e, "extraction_class", None)
        # str(): lot_index arrives as an int on some extractions, and the
        # sorted() calls below raise TypeError on a mixed int/str set.
        li = str((getattr(e, "attributes", None) or {}).get("lot_index") or "1")
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
            attrs = getattr(e, "attributes", None) or {}
            gran_by_lot.setdefault(li, []).append((sp, txt, c, attrs))

    missing_fd, incomplete = [], {}
    for li, spans in gran_by_lot.items():
        fd = fd_by_lot.get(li)
        if fd is None or (fd["span"] is None and not fd["text"]):
            missing_fd.append(li)
            continue
        outside = set()
        for sp, txt, cls, attrs in spans:
            by_span = (sp and fd["span"] and fd["span"][0] <= sp[0] <= sp[1] <= fd["span"][1])
            by_text = (txt and fd["text"] and txt in fd["text"])
            by_tokens = False
            if txt and fd["text"] and not (by_span or by_text):
                toks = _cov_tokens(txt)
                block = fd["text"].lower()
                missing = [t for t in toks if t not in block]
                # The state is derivable from the district (place resolution
                # fills it in) and notices routinely omit it, so a state the
                # description never spells out is not evidence of truncation.
                exempt = set(_cov_tokens(str(attrs.get("state") or "")))
                by_tokens = bool(toks) and all(t in exempt for t in missing)
            if not (by_span or by_text or by_tokens):
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
    prop_lots: set = set()          # lots with a property entity
    prop_type: dict = {}            # lot_index -> property_type
    possession: dict = {}           # lot_index -> possession_type value
    extent_lots: set = set()        # lots with any extent entity
    uds_lots: set = set()           # lots whose extent carries an undivided_share
    borrower_lots: set = set()      # lots with a borrower

    for e in extractions:
        a = e.attributes or {}
        c = e.extraction_class
        classes[c] += 1
        present_fields.add(c)             # class presence (borrower/location/...)
        li = str(a.get("lot_index") or "1")   # see full_description_coverage
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
            extent_lots.add(li)
            if a.get("undivided_share"):
                uds_lots.add(li)
            p = _num(a.get("uds_parent_extent"))
            if p is not None:
                uds_parent.setdefault(li, set()).add(round(p, 2))
            for k in ("total_area", "extent_sqft"):
                v = _num(a.get(k))
                if v is not None:
                    own_area.setdefault(li, set()).add(round(v, 2))
        elif c == "property":
            prop_lots.add(li)
            if a.get("property_type"):
                prop_type[li] = a["property_type"]
            if a.get("possession_type"):
                possession[li] = a["possession_type"]
        elif c == "borrower":
            borrower_lots.add(li)

    # ── core completeness ────────────────────────────────────────────────────
    if not classes.get("secured_creditor"):
        flag("missing_secured_creditor", "high", "no secured_creditor entity")
    if not classes.get("borrower"):
        flag("missing_borrower", "high", "no borrower entity")   # lot anchor
    if not classes.get("location"):
        flag("missing_location", "med", "no location entity")
    if not reserves:
        flag("missing_reserve_price", "high",                    # lot anchor
             "no reserve_price_num in any auction_terms")
    if not classes.get("extent"):
        flag("missing_extent", "high", "no extent entity")       # priority field

    # ── priority fields (reviewer-weighted) ──────────────────────────────────
    # property_type: high — a property block with no type is barely usable.
    no_type = sorted(li for li in prop_lots if not prop_type.get(li))
    if no_type:
        flag("missing_property_type", "high",
             f"property lot(s) {no_type} have no property_type")
    # possession_type: high, but only when PRESENT and invalid (Option A).
    bad_poss = sorted(li for li, p in possession.items()
                      if str(p).strip().lower() not in _POSSESSION_VALID)
    if bad_poss:
        flag("possession_type_invalid", "high",
             f"lot(s) {bad_poss} possession_type not one of {sorted(_POSSESSION_VALID)}")
    # UDS: a flat owns an undivided share of land — high when it's missing.
    flat_no_uds = sorted(li for li in prop_lots
                         if "flat" in str(prop_type.get(li, "")).lower()
                         and li not in uds_lots)
    if flat_no_uds:
        flag("missing_uds", "high",
             f"flat lot(s) {flat_no_uds} have no undivided_share (UDS) extent")
    # Lot anchors on a MULTI notice: every property lot needs its own reserve +
    # borrower, else a lot isn't fully captured. Count-based (robust to lot_index
    # mis-tagging): fewer anchors than property lots -> a lot is missing one.
    n_prop_lots = len(prop_lots)
    if n_prop_lots > 1:
        if len(reserves) < n_prop_lots:
            flag("lot_missing_reserve", "high",
                 f"{n_prop_lots} property lots but only {len(reserves)} reserve price(s)")
        if len(borrower_lots) < n_prop_lots:
            flag("lot_missing_borrower", "high",
                 f"{n_prop_lots} property lots but only {len(borrower_lots)} with a borrower")

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
    bad_reserves = sorted((li, r) for li, r in reserves.items()
                          if not (_RESERVE_MIN <= r <= _RESERVE_MAX))
    if bad_reserves:
        flag("reserve_out_of_range", "med", "implausible reserve(s): " + ", ".join(
            f"lot {li}={r:.0f}" for li, r in bad_reserves))
    bad_emd = sorted((li, emds[li] / r) for li, r in reserves.items()
                     if emds.get(li) and r
                     and not (_EMD_LO <= emds[li] / r <= _EMD_HI))
    if bad_emd:
        flag("emd_ratio_off", "low", "emd/reserve off (expect ~0.10): " + ", ".join(
            f"lot {li}={ratio:.2f}" for li, ratio in bad_emd))
    # A parent-plot extent must live ONLY in uds_parent_extent — never be echoed
    # as the property's own area. Overlap means the whole plot got recorded as
    # the property's size (e.g. a 760 sq.ft flat shown as 2257 sq.ft). This is
    # most common for flats but is NOT flat-specific: any "X out of Y" parcel
    # (land + building included) hits it, so the message stays type-neutral.
    uds_overlap = {li: sorted(parents & own_area.get(li, set()))
                   for li, parents in uds_parent.items()
                   if parents & own_area.get(li, set())}
    if uds_overlap:
        flag("uds_parent_as_own_area", "high",
             "lot(s) " + "; ".join(f"{li}: {vals}" for li, vals
                                   in sorted(uds_overlap.items()))
             + " record the parent-plot extent as the property's own area "
               "(total_area/extent_sqft) — the parcel sold is a share OF that "
               "plot, so the figure belongs only in uds_parent_extent")

    # ── multi-lot recall heuristic ───────────────────────────────────────────
    if source_text:
        # DISTINCT serial numbers, not raw hits: a notice restates "For S.No.1"
        # in its contact block, and counting those back-references as separate
        # lots made a correctly-extracted 2-lot notice look under-recalled.
        markers = len({int(m) for m in _LOT_MARKER.findall(source_text)})
        # crude: many lot markers but few distinct lots extracted -> under-recall
        if markers >= 3 and len(lots) * 2 < markers:
            flag("lot_under_recall", "med",
                 f"{markers} lot markers in source but only {len(lots)} lot(s) extracted")

    # ── full_description completeness (the single most-weighted field) ────────
    cov = full_description_coverage(extractions)
    if cov["lots_missing_full_description"]:
        flag("missing_full_description", "critical",
             f"lot(s) {cov['lots_missing_full_description']} have property details "
             f"but no full_description block")
    if cov["lots_incomplete"]:
        detail = "; ".join(f"lot {li}: {cls}" for li, cls in cov["lots_incomplete"].items())
        flag("full_description_incomplete", "high",
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


def validate_stored(entities: list[dict], source_text: str = "") -> dict:
    """validate() for entities already persisted as Document.extraction_json dicts
    ({id, cls, text, start, end, attrs}), e.g. for a from-graph batch report
    (pipeline/extract_batch.py --from-graph) or backfilling a score onto
    Documents extracted before scoring was tracked (scripts/backfill_extraction_scores.py).

    Shims each dict to the attribute shape validate() expects (extraction_class /
    attributes / char_interval) — no LLM call, pure re-validation of stored output.
    """
    from types import SimpleNamespace
    shims = [SimpleNamespace(
        extraction_class=e.get("cls"),
        extraction_text=e.get("text") or "",
        attributes=e.get("attrs") or {},
        char_interval=None if e.get("start") is None else SimpleNamespace(
            start_pos=e.get("start"), end_pos=e.get("end")),
    ) for e in entities]
    return validate(shims, source_text=source_text)
