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

# Bump on ANY change that moves a score for unchanged entities: a penalty
# weight, a check's severity, a new check, a loosened or tightened rule.
#
# Every write path stamps this beside the score (Document.extraction_score_version,
# see pipeline/load_extractions.py and scripts/reset_langextract_and_extract.py),
# because a stored score alone is not self-describing. Without the stamp a
# weights change silently rewrites the meaning of every historical score: a
# corpus is then a mix of scales that look identical, "mean score went up" can
# be a validators.py edit rather than a better extraction, and a score filter in
# the review queue selects different documents depending on when each was
# extracted. With it, a mixed corpus can be told apart and re-levelled —
# `python -m scripts.backfill_extraction_scores` rescores everything behind the
# current version, with no LLM call.
SCORE_VERSION = 6   # 4: full_description_incomplete stops charging details
                    # that are not a truncation; detail_wrong_lot (med) added
                    # 5: one span tagged to several lots is the nearest lot's
                    # 6: wrong_lot only when the lot has its own such detail
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


def _alnum(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


# ── what a detail outside full_description does NOT prove ────────────────────
# A sweep of all 841 notices this check flagged (2026-10) sorted the 2,952
# details it had found "outside the description". 652 of the notices (78%) were
# flagged only by details that are no sign of a cut-short block:
#   - the same fact written another way ("D.No. 92/2" outside, "Door No.92/2"
#     inside), or a quote the model shortened with "..."      545
#   - a record ABOUT the property, not a detail of it: the bank portal's
#     property ID, CERSAI ID, deed number, GPS coordinates, the borrower's
#     home address, a possession / encumbrance status           811
#   - a detail lying inside ANOTHER lot's description: the lot tag is wrong,
#     the block is not short                                    419
#   - the heading line just before the block ("Item No.1: …"), or a quote
#     straddling its edge                                       246
#   - a repeat in the notice's "Property address" box or summary table   246
# The remaining 685 details — boundaries after "bounded by:", a Schedule B / C,
# the UDS — are the real truncations this check exists for, and stay flagged.

# Identifier kinds that name a record about the property, not a detail of it.
_RECORD_KINDS = frozenset({"property_id", "cersai", "sale_deed"})
_RECORD_TEXT = re.compile(r"property\s*id|cersai", re.I)
_COORDS = re.compile(r"latitude|longitude|\b\d{1,3}\.\d{4,}\b", re.I)
_STATUS_TEXT = re.compile(r"possession|encumbrance", re.I)
# What the text just before a detail says about whose address it is.
_PARTY_ADDRESS = re.compile(r"residing|\br/o\b|also at|guarantor|borrower", re.I)
_PROPERTY_WORD = re.compile(r"propert|mortgaged|schedule|item", re.I)
_ADDRESS_BOX = re.compile(
    r"property\s*address|address\s*of\s*(the\s*)?(mortgaged\s*)?property", re.I)
_TABLE_CELL = re.compile(r"</?t[dh]\b", re.I)
_NUM_TOKEN = re.compile(r"[a-z0-9/\-]*\d[a-z0-9/\-]*")
_ORDINAL = re.compile(r"^\d+(st|nd|rd|th)$")
_PLACE_WORD = re.compile(r"[a-z]{4,}")
_GENERIC_WORDS = frozenset({
    "village", "taluk", "taluka", "thaluka", "district", "registration", "limit",
    "limits", "within", "situated", "situate", "corporation", "municipality",
    "panchayat", "town", "city", "road", "street", "north", "south", "east",
    "west", "nagar", "ward", "block", "door", "plot", "flat", "floor", "survey",
    "property", "land", "building", "tamil", "nadu", "tamilnadu", "near"})


def _strong_numbers(text: str) -> set:
    """Number tokens distinctive enough to identify a fact: "92/2", "4622",
    "f3" — not a lone digit or an ordinal like "1st"."""
    toks = {n.strip("-/") for n in _NUM_TOKEN.findall(text)}
    return {n for n in toks if len(n) >= 2 and not _ORDINAL.match(n)}


def _same_fact(text: str, fd_text: str) -> bool:
    """The detail is in the block, worded differently: every distinctive
    number of it appears in the block as a whole token, or — for a detail with
    no number, a place — every place name does. A detail with only weak numbers
    ("Plot No.5") cannot be told apart from any other and is not matched."""
    if re.search(r"\d", text):
        strong = _strong_numbers(text)
        return bool(strong) and strong <= {n.strip("-/") for n in _NUM_TOKEN.findall(fd_text)}
    words = {w for w in _PLACE_WORD.findall(text) if w not in _GENERIC_WORDS}
    return bool(words) and words <= set(_PLACE_WORD.findall(fd_text))


def _covered_by_text(txt: str, cls: str, fd: dict) -> bool:
    """Is the detail's text derivable from the block's text, wherever it sits?"""
    if not txt or not fd["text"]:
        return False
    if txt in fd["text"]:
        return True
    squashed = _alnum(txt)
    if len(squashed) >= 5 and squashed in fd["alnum"]:
        return True                        # same words, other spacing/punctuation
    pieces = [_alnum(p) for p in re.split(r"\.\.\.|…", txt)]
    pieces = [p for p in pieces if len(p) >= 5]
    if len(pieces) > 1 and all(p in fd["alnum"] for p in pieces):
        return True                        # the model's "…" elided the middle
    # A boundary names its NEIGHBOURS ("Plot No.14"), whose numbers a block
    # often states for other reasons, so it is never matched on numbers.
    return cls != "boundary" and _same_fact(txt, fd["text"])


def _gap(span, block) -> int:
    """Characters between a span and a block (0 when they touch or overlap)."""
    (s, t), (a, b) = span, block
    return max(0, a - t, s - b)


def _shared_home(span, lot, fd_span, sharers: dict) -> str | None:
    """The lot a detail belongs to when the model tagged the very same span to
    several lots: the one whose description block lies nearest it. ``sharers``
    maps each OTHER lot carrying this span to its block. None when that is
    ``lot`` itself (or nobody else carries it)."""
    if not sharers:
        return None
    best = min([(lot, fd_span)] + sorted(sharers.items()), key=lambda kv: _gap(span, kv[1]))
    return None if best[0] == lot else best[0]


def _has_own(items, span, cls, kind) -> bool:
    """Does the lot carry another detail of this class (and identifier kind)
    at a different place? Only then is a copy that belongs elsewhere a
    mislabel: a lot whose only village is the one the notice states for all
    its lots is sharing it. A 2026-10 dry run that dropped such copies would
    have left 80 of 96 notices with lots that had no village or taluk."""
    return any(c == cls and (cls != "identifier" or k == kind) and sp and sp != span
               for sp, _t, c, k, *_ in items)


def _outside_reason(span, txt, cls, kind, lot, fd, other_blocks, source_text,
                    sharers: dict | None = None):
    """Why a placed detail outside its lot's block is not a truncation, or None
    when it is one. "covered" means the block does hold it after all;
    "wrong_lot" that it sits in another lot's block, or that the same span is
    tagged to another lot whose block lies nearer; anything else names the
    kind of record it is (reported, not scored)."""
    s, t = span
    a, b = fd["span"]
    if any(x <= s and t <= y for other, (x, y) in other_blocks if other != lot):
        return "wrong_lot"
    if _shared_home(span, lot, fd["span"], sharers or {}):
        return "wrong_lot"
    if s < b and t > a:
        return "covered"                   # straddles the block's edge
    if cls in ("property", "schedule") and t <= a and a - t <= 5:
        return "covered"                   # the heading line right before it
    if cls == "identifier" and (kind in _RECORD_KINDS or _RECORD_TEXT.search(txt)):
        return "record_id"
    if cls == "location" and _COORDS.search(txt):
        return "coordinates"
    if cls == "property" and len(txt) < 60 and _STATUS_TEXT.search(txt):
        return "status"
    if source_text:
        before = source_text[max(0, s - 120):s]
        if (cls in ("location", "property") and _PARTY_ADDRESS.search(before)
                and not _PROPERTY_WORD.search(before[-80:])):
            return "party_address"
        if _ADDRESS_BOX.search(before):
            return "address_box"
        if _TABLE_CELL.search(before):
            return "table"
    return None


def full_description_coverage(extractions, source_text: str = "") -> dict:
    """Per-lot check that full_description is the complete source of truth.

    Design rule: full_description is the verbatim union of a lot's descriptive
    detail, so every survey/identifier, village/taluk/district, extent and
    boundary must be DERIVABLE from it. A descriptive entity counts as covered
    when its char span sits INSIDE that lot's full_description span OR its text
    appears within the full_description text (the text arm forgives a value that
    is merely repeated at a second position outside the block — still derivable).
    An entity covered by neither means full_description was truncated before that
    detail, so the field can no longer be derived from it.

    Containment needs two spans. When either the entity or the lot's
    full_description is ungrounded, "outside the block" is not a finding — it is
    the absence of one, and charging it as truncation bills the missing span
    twice: once as ``ungrounded``, again as ``full_description_incomplete``.
    In a 150-document sample of multi-lot notices that was 52% of the entities
    this check reported, and 14 of the 80 documents it flagged had no other
    reason to be there — they were charged 20 points for a missing span. Such an
    entity is reported separately as ``lots_unverifiable`` — visible for triage,
    scored by the flag that owns it.

    A detail that is placed outside the block is still not always a sign of a
    cut-short block — see the sweep above ``_RECORD_KINDS``. The text arm also
    matches the same words with other spacing, a quote the model elided with
    "...", and the same fact worded differently (``_same_fact``); a placed
    detail is then tested by ``_outside_reason``. One lying inside ANOTHER
    lot's block is reported as ``lots_wrong_lot`` (its own flag, a tagging
    defect); a record about the property rather than a detail of it (portal
    ID, CERSAI, coordinates, the borrower's address, a repeat in the address
    box or a table) as ``lots_excused`` — visible for triage, not scored.
    ``source_text`` lets the address / table tests read what precedes a detail;
    without it they are skipped and such details stay incomplete.

    Returns per-notice aggregates: lots that have descriptive spans but no
    full_description, lots with detail falling outside it (offending classes),
    lots whose coverage could not be checked (same shape), lots carrying another
    lot's detail (same shape), and lots with excused detail
    ({lot: {reason: [classes]}}).
    Entities with neither a span nor text are skipped (nothing to check).
    """
    fd_by_lot: dict = {}          # lot -> {"span", "spans", "text", "alnum"}
    gran_by_lot: dict = {}        # lot -> [(span|None, text, cls, kind), ...]
    for e in extractions:
        c = getattr(e, "extraction_class", None)
        # str(): the model emits lot_index as a number about as often as a
        # string, and the "1" default is a string. Mixing the two makes the
        # `sorted(missing_fd)` below raise TypeError, which kills the whole
        # extraction — pipeline.apply_extractions.group_lots already normalises
        # the same way, so this only brings the validator into line with it.
        attrs = getattr(e, "attributes", None) or {}
        li = str(attrs.get("lot_index") or "1")
        sp = _span_of(e)
        txt = _norm_ws(getattr(e, "extraction_text", ""))
        if c == "full_description":
            slot = fd_by_lot.setdefault(li, {"span": None, "spans": [], "text": ""})
            if sp:
                slot["span"] = ((min(slot["span"][0], sp[0]), max(slot["span"][1], sp[1]))
                                if slot["span"] else sp)
                slot["spans"].append(sp)
            if txt:
                slot["text"] = (slot["text"] + " " + txt).strip()
        elif c in _DESCRIPTION_CLASSES and (sp or txt):
            kind = normalize_identifier_kind(attrs.get("kind"))[0] if c == "identifier" else None
            gran_by_lot.setdefault(li, []).append(
                (sp, txt, c, kind, getattr(e, "id", None),
                 str(getattr(e, "extraction_text", "") or "")))
    for slot in fd_by_lot.values():
        slot["alnum"] = _alnum(slot["text"])
    other_blocks = [(li, sp) for li, slot in fd_by_lot.items() for sp in slot["spans"]]
    # The same span tagged to several lots (a table row's village copied onto
    # every lot): who else carries it, with their blocks — see _shared_home.
    span_lots: dict = {}
    for li, items in gran_by_lot.items():
        for sp, _t, c, *_ in items:
            if sp:
                span_lots.setdefault((c, sp), set()).add(li)

    missing_fd, incomplete, unverifiable, wrong_lot, excused = [], {}, {}, {}, {}
    details: dict = {}            # lot -> [{kind, cls, text, id, in_lot}]
    for li, items in gran_by_lot.items():
        fd = fd_by_lot.get(li)
        if fd is None or (fd["span"] is None and not fd["text"]):
            missing_fd.append(li)
            continue
        outside, unchecked, elsewhere, reasons = set(), set(), set(), {}
        for sp, txt, cls, kind, eid, raw in items:
            by_span = (sp and fd["span"] and fd["span"][0] <= sp[0] <= sp[1] <= fd["span"][1])
            if by_span or _covered_by_text(txt, cls, fd):
                continue
            if not (sp and fd["span"]):
                unchecked.add(cls)     # no span to place it by — see docstring
                continue
            sharers = {o: fd_by_lot[o]["span"] for o in span_lots.get((cls, sp), ())
                       if o != li and fd_by_lot.get(o, {}).get("span")}
            why = _outside_reason(sp, txt, cls, kind, li, fd, other_blocks, source_text,
                                  sharers)
            if why is None:
                outside.add(cls)       # both placed, and it really is outside
                details.setdefault(li, []).append(
                    {"kind": "incomplete", "cls": cls, "text": raw, "id": eid, "in_lot": None})
            elif why == "wrong_lot" and not _has_own(items, sp, cls, kind):
                # The lot has no detail of this kind but this one: a fact
                # the notice states once for several lots ("all the
                # properties below are in Zuzuvadi village"), which the
                # pipeline copies onto each on purpose (gap_fill.
                # inherit_shared). Sharing, not a mislabel — see _has_own.
                reasons.setdefault("shared", set()).add(cls)
            elif why == "wrong_lot":
                elsewhere.add(cls)
                home = next((o for o, (x, y) in other_blocks
                             if o != li and x <= sp[0] and sp[1] <= y), None) \
                    or _shared_home(sp, li, fd["span"], sharers)
                details.setdefault(li, []).append(
                    {"kind": "wrong_lot", "cls": cls, "text": raw, "id": eid, "in_lot": home})
            elif why != "covered":
                reasons.setdefault(why, set()).add(cls)
        if outside:
            incomplete[li] = sorted(outside)
        if unchecked:
            unverifiable[li] = sorted(unchecked)
        if elsewhere:
            wrong_lot[li] = sorted(elsewhere)
        if reasons:
            excused[li] = {k: sorted(v) for k, v in sorted(reasons.items())}
    return {
        "lots_with_description": len(gran_by_lot),
        "lots_missing_full_description": sorted(missing_fd),
        "lots_incomplete": incomplete,
        "lots_unverifiable": unverifiable,
        "lots_wrong_lot": wrong_lot,
        "lots_excused": excused,
        # Which details, per lot — what the review page lists under a lot's
        # description so a reviewer sees the gap without hunting for it.
        "details": details,
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
    location_lots: set = set()      # lots with a location entity

    for e in extractions:
        a = e.attributes or {}
        c = e.extraction_class
        classes[c] += 1
        present_fields.add(c)             # class presence (borrower/location/...)
        # str() for the same reason as in full_description_coverage: without it
        # a notice whose entities carry both 1 and "1" counts as two lots.
        li = str(a.get("lot_index") or "1")
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
            elif a.get("property_type_state") == "not_stated":
                # The reader (pipeline/reader) read the lot and found no type
                # stated — an honest absence, recorded as such, not a miss.
                # The key checklist still shows the cell as missing until a
                # person marks it absent; the score does not charge it twice.
                prop_lots.discard(li)
            if a.get("possession_type"):
                possession[li] = a["possession_type"]
        elif c == "borrower":
            borrower_lots.add(li)
        elif c == "location":
            location_lots.add(li)

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
    # Lot anchors on a MULTI notice: every property lot needs its own reserve,
    # borrower and location, else a lot isn't fully captured. Count-based
    # (robust to lot_index mis-tagging): fewer anchors than property lots ->
    # a lot is missing one.
    #
    # `location` is here as well as in core completeness above because the
    # notice-level check only fires when a notice has NO location at all, and
    # that is not how this fails on a long notice: the model places the first
    # few lots and then stops. 188 lots across 56 notices carry no location of
    # their own while the notice around them scored clean — one is a 40-lot
    # notice with four locations. A lot with no location cannot be placed, so
    # it cannot be searched, filtered or priced, which is the whole product.
    n_prop_lots = len(prop_lots)
    if n_prop_lots > 1:
        if len(reserves) < n_prop_lots:
            flag("lot_missing_reserve", "high",
                 f"{n_prop_lots} property lots but only {len(reserves)} reserve price(s)")
        if len(borrower_lots) < n_prop_lots:
            flag("lot_missing_borrower", "high",
                 f"{n_prop_lots} property lots but only {len(borrower_lots)} with a borrower")
        # `location_lots` must be non-empty: a notice with no location at all
        # is already charged once by the notice-level check above, and charging
        # it again here would both double-penalise one defect and make the
        # score a function of lot count — the invariant tests/api/
        # test_score_lot_normalization.py exists to hold.
        if location_lots and len(location_lots) < n_prop_lots:
            flag("lot_missing_location", "high",
                 f"{n_prop_lots} property lots but only {len(location_lots)} "
                 f"with a location")

    # ── grounding / cleanliness ──────────────────────────────────────────────
    if ungrounded:
        # Severity tracks how much of the notice lost its anchor, because the
        # defect is not binary: one paraphrased value among two hundred good
        # ones is a blemish, while a fifth of the page unplaceable means the
        # model stopped quoting and started composing. A flat penalty charged
        # both the same — the corpus median is 4% of a document's entities, and
        # that was costing a full med (see _PENALTY), which left the check no
        # gradient for the loop this module exists to drive.
        frac = ungrounded / max(1, sum(classes.values()))
        sev = "low" if frac < 0.05 else ("med" if frac < 0.20 else "high")
        flag("ungrounded", sev,
             f"{ungrounded} of {sum(classes.values())} extraction(s) not "
             f"grounded to source ({frac:.0%})")
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
    # A flat's UDS parent-plot extent must live ONLY in uds_parent_extent — never
    # be echoed as the property's own area. Overlap means the whole plot got
    # recorded as the flat's size (e.g. a 760 sq.ft flat shown as 2257 sq.ft).
    uds_overlap = {li: sorted(parents & own_area.get(li, set()))
                   for li, parents in uds_parent.items()
                   if parents & own_area.get(li, set())}
    if uds_overlap:
        flag("uds_parent_as_own_area", "high",
             "lot(s) " + "; ".join(f"{li}: {vals}" for li, vals
                                   in sorted(uds_overlap.items()))
             + " record the UDS parent extent as the property's own area "
               "(total_area/extent_sqft) — for a flat that value belongs only in "
               "uds_parent_extent")

    # ── multi-lot recall heuristic ───────────────────────────────────────────
    if source_text:
        markers = len(_LOT_MARKER.findall(source_text))
        # crude: many lot markers but few distinct lots extracted -> under-recall
        if markers >= 3 and len(lots) * 2 < markers:
            flag("lot_under_recall", "med",
                 f"{markers} lot markers in source but only {len(lots)} lot(s) extracted")

    # ── full_description completeness (the single most-weighted field) ────────
    cov = full_description_coverage(extractions, source_text)
    if cov["lots_missing_full_description"]:
        flag("missing_full_description", "critical",
             f"lot(s) {cov['lots_missing_full_description']} have property details "
             f"but no full_description block")
    if cov["lots_incomplete"]:
        detail = "; ".join(f"lot {li}: {cls}" for li, cls in cov["lots_incomplete"].items())
        flag("full_description_incomplete", "high",
             f"full_description does not cover all descriptive spans ({detail})")
    # A detail sitting inside another lot's description block: that block is
    # not short, the lot tag is wrong — a lot then carries a neighbour's survey
    # number or boundary. Med, not high: every value is still on the notice.
    if cov["lots_wrong_lot"]:
        detail = "; ".join(f"lot {li}: {cls}" for li, cls in cov["lots_wrong_lot"].items())
        flag("detail_wrong_lot", "med",
             f"detail tagged to one lot sits in another lot's description ({detail})")

    score = max(0, 100 - sum(_PENALTY[i["severity"]] for i in issues))
    return {
        "score": score,
        "score_version": SCORE_VERSION,
        "issues": issues,
        "fields": sorted(present_fields),
        "stats": {
            "n_extractions": sum(classes.values()),
            "classes": dict(classes),
            "lots": len(lots),
            "ungrounded": ungrounded,
            "null_values": nullvals,
            "full_description_incomplete_lots": len(cov["lots_incomplete"]),
            "full_description_unverifiable_lots": len(cov["lots_unverifiable"]),
            "full_description_wrong_lot_lots": len(cov["lots_wrong_lot"]),
            "full_description_excused_lots": len(cov["lots_excused"]),
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
    return validate(shim_stored(entities), source_text=source_text)


def shim_stored(entities: list[dict]) -> list:
    """Stored entity dicts in the attribute shape validate() reads; ``id`` is
    the entity's stored id (or its position), so a finding can point at it."""
    from types import SimpleNamespace
    return [SimpleNamespace(
        id=e.get("id") or str(i),
        extraction_class=e.get("cls"),
        extraction_text=e.get("text") or "",
        attributes=e.get("attrs") or {},
        char_interval=None if e.get("start") is None else SimpleNamespace(
            start_pos=e.get("start"), end_pos=e.get("end")),
    ) for i, e in enumerate(entities) if isinstance(e, dict)]
