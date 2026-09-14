"""Cross-portal matching: is this incoming listing an auction we already hold?

Pure functions over dicts — no network, no graph. The caller (``scripts/gap_report.py``
and ``scripts/link_listings.py``) fetches what the graph knows and hands it in
beside the harvested rows.

Two listings are only ever compared across *different* sources, inside a
bucket: canonical bank (``pipeline.entity_resolution.org_key``) and auction
calendar day, with reserve prices agreeing within
``pipeline.price_agreement.TOLERANCE_PCT`` (or one side unpriced).

A bucket routinely holds a same-day batch sale — one borrower, several
properties, often one price (BAANKNET ``bn-351743`` / ``bn-351740``). So the
rules are the notice-lot matcher's (``pipeline.apply_extractions.match_lots_to_listings``):

* each listing gets at most ONE partner per other portal; evidence narrows the
  candidates, strongest first, and must reach exactly one;
* a tie that survives every tier stays unresolved (``Ambiguity``), never guessed;
* an identifier held by more than one listing on either side is shared ground
  (the land under a batch), not evidence; a short unit number (``G1``) counts
  only when the borrower agrees or a second identifier does;
* a pair stands only when both listings choose each other — otherwise both
  are left unresolved;
* identical postings of one unit on one portal are one candidate.

    notice_bytes   the same sale-notice file on both sides       CONFIRMED
    boundaries     three of four neighbours agree                CONFIRMED
    identifier     the same unique survey / door / plot / flat / villa number   PROBABLE
    borrower       the same party (token_set_ratio >= 90)        PROBABLE
    bucket_only    the one price-agreeing listing in the bucket  INFERRED
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Iterable

from pipeline.entity_resolution import normalize, org_key
from pipeline.lot_resolution import BORROWER_MATCH_MIN_SCORE, _round_reserve
from pipeline.match_confidence import listing_confidence_for
from pipeline.price_agreement import compare_prices

SIDES = ("north", "south", "east", "west")

# ── candidates ───────────────────────────────────────────────────────────────


@dataclass
class Candidate:
    """One listing, from either side of the comparison, reduced to the
    evidence the matcher reads. Build with :func:`candidate_from_row` (a
    harvested ``data/listings`` row) or :func:`candidate_from_graph` (a record
    fetched from Neo4j)."""
    auction_id: str
    source: str
    bank: str = ""
    reserve_price_num: float | None = None
    auction_start_dt: str | None = None
    borrower: str = ""
    boundaries: dict[str, str] = field(default_factory=dict)      # side → neighbour text
    identifiers: set[tuple[str, str]] = field(default_factory=set)  # (family, value_norm)
    doc_shas: set[str] = field(default_factory=set)

    @property
    def bank_key(self) -> str:
        return org_key(self.bank or "")

    @property
    def day(self) -> str | None:
        return day_of(self.auction_start_dt)

    @property
    def bucket_key(self) -> tuple[str, str] | None:
        """Bank + day. Price is checked pairwise (tolerance), not keyed, so a
        listing without a published reserve price still lands in its bucket
        — it can then be matched on evidence, never on the bucket alone."""
        if not self.bank_key or not self.day:
            return None
        return self.bank_key, self.day

    @property
    def has_price(self) -> bool:
        return _round_reserve(self.reserve_price_num) not in (None, 0)


def day_of(value) -> str | None:
    """``'2026-09-24T11:00:00'`` → ``'2026-09-24'``; Neo4j datetimes and
    dates too. ``None`` when there is no usable date."""
    if value is None:
        return None
    if hasattr(value, "year"):                       # datetime / date / neo4j temporal
        try:
            return f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
        except (AttributeError, TypeError):
            return None
    s = str(value).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
        return s[:10]
    return None


# ── boundaries from free text ────────────────────────────────────────────────
#
# Portals give the four neighbours inside the description, three ways seen in
# the 2026-09-12 harvest:
#   "bounded on west by property belongs to Narayanan, east by road, south by …"
#   "North : Property of Mr. X, South : 20 feet road, East : …"
#   "North-Plot No.12, South-Road, East-…"
# The neighbour text runs to the next side word or the end of the sentence.

_SIDE_WORD = r"(?:north|south|east|west)"
# A side word, a separator, then the neighbour up to the next comma,
# semicolon, end of text, or the next side word with its own separator
# (not a full stop: "Property of Mr. Kumar" has one in the middle).
# "northern side 37 feet" never matches: "northern" is one word, so \b fails.
_BOUNDARY = re.compile(
    rf"\b(north|south|east|west)\b\s*(?:by|:|-|–|—)\s*"
    rf"(?P<val>[^,;]+?)"
    rf"(?=[,;]|$|\.?\s+(?:and\s+)?(?:on\s+the\s+)?{_SIDE_WORD}\b\s*(?:by|:|-|–|—))",
    re.IGNORECASE,
)


_STARTS_WITH_SIDE = re.compile(rf"^{_SIDE_WORD}(?:\s|$)")


def extract_boundaries(text: str | None) -> dict[str, str]:
    """``{'north': 'property belongs to p kumar', 'east': 'road', …}`` from a
    portal description; sides not stated are absent. Values are normalised
    for comparison, never shown."""
    out: dict[str, str] = {}
    if not text:
        return out
    for m in _BOUNDARY.finditer(text):
        side = m.group(1).lower()
        if side in out:
            continue
        val = normalize(m.group("val").strip(" ,;.-"))
        # "North-East by plot 7" / "north to south 19 feet": a side word as the
        # neighbour is a compound direction or a dimension, not a boundary.
        if val and not _STARTS_WITH_SIDE.match(val):
            out[side] = val
    return out


def boundary_matches(a: dict[str, str], b: dict[str, str], *, min_sides: int = 3) -> bool:
    """True when at least ``min_sides`` sides agree. A side agrees when the
    normalised neighbour text is equal or one contains the other ("road" in
    "20 feet road"); two blanks never agree."""
    agreed = 0
    for side in SIDES:
        x, y = a.get(side), b.get(side)
        if not x or not y:
            continue
        if x == y or _contains(x, y):
            agreed += 1
    return agreed >= min_sides


def _contains(x: str, y: str) -> bool:
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    return len(short) >= 4 and f" {short} " in f" {long_} "


# ── identifiers from free text ───────────────────────────────────────────────
#
# Kinds follow ``:Identifier.kind`` in the graph (survey_old / survey_new /
# door_old / door_new / plot / flat), collapsed to a family so an old survey
# number on one side matches a survey number of unstated age on the other.

_FAMILY = {"survey_old": "survey", "survey_new": "survey", "survey": "survey",
           "door_old": "door", "door_new": "door", "door": "door",
           "plot": "plot", "flat": "flat", "villa": "villa"}

_IDENT = re.compile(
    r"\b(?P<kind>"
    r"(?:old\s+|new\s+|t\.?\s*s\.?\s*|r\.?\s*s\.?\s*|re-?survey\s*|survey\s+|s\.?\s*)no\.?s?"
    r"|(?:old\s+|new\s+)?(?:door|d\.?)\s*no\.?s?"
    r"|plot\s+no\.?s?"
    r"|flat(?:\s+no\.?s?)?"
    r"|villa(?:\s+no\.?s?)?"
    r")\s*[:.\-]?\s*"
    r"(?P<value>(?:\d+[A-Za-z]?|[A-Za-z]{1,2}\d+)(?:\s*(?:/|by|-)\s*\d*[A-Za-z]?\d*)*)"
    r"(?![a-z])"
    r"(?![\d.]*\s*(?:sq|sft|cents?\b|acres?\b))",
    re.IGNORECASE,
)


def identifier_family(kind: str | None) -> str | None:
    return _FAMILY.get((kind or "").lower())


def normalize_identifier_value(value: str | None) -> str:
    """``'381 BY 5A'`` → ``'381/5a'``; ``'300/3'`` stays; spaces and case go."""
    if not value:
        return ""
    v = re.sub(r"\s*(?:by|-)\s*", "/", value.strip(), flags=re.IGNORECASE)
    v = re.sub(r"\s+", "", v).lower().strip("/")
    return v


def extract_identifiers(text: str | None) -> set[tuple[str, str]]:
    """``{('survey', '381/5a'), ('door', '81a/1')}`` from a portal description.
    Bare single-digit values are dropped: "S.No 3" is too common to be
    evidence on its own."""
    out: set[tuple[str, str]] = set()
    if not text:
        return out
    # A neighbour's number ("bounded north by Plot No 28") identifies the
    # neighbour, not this property: anything inside a boundary clause is skipped.
    neighbour_spans = [m.span("val") for m in _BOUNDARY.finditer(text)]
    for m in _IDENT.finditer(text):
        if any(lo <= m.start() < hi for lo, hi in neighbour_spans):
            continue
        kind = m.group("kind").lower()
        if "villa" in kind:
            fam = "villa"
        elif "plot" in kind:
            fam = "plot"
        elif "flat" in kind:
            fam = "flat"
        elif kind.startswith("d") or "door" in kind:
            fam = "door"
        else:
            fam = "survey"
        val = normalize_identifier_value(m.group("value"))
        if len(val.replace("/", "")) < 2:
            continue
        out.add((fam, val))
    return out


# ── extent from free text ────────────────────────────────────────────────────
#
# "total extent 1215 sqft", "684 sq.ft or 63.54 sq.mts", "2.17 Cents",
# "1 acre 20 cents": the first area phrase a portal description states.

_EXTENT = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:sq\.?\s*(?:ft|feet|m|mt|mts|mtr|mtrs|metres?|meters?|yards?|yds?)\b"
    r"|sqft|sqm|sq\.?\s*ft|cents?\b|acres?\b|ares?\b|grounds?\b|hectares?\b|ha\b)",
    re.IGNORECASE)


def extract_extent(text: str | None) -> str | None:
    """The first area phrase in ``text`` (``'1215 sqft'``), or ``None``. A
    statement, not a measurement: units are not converted here."""
    if not text:
        return None
    m = _EXTENT.search(text)
    return m.group(0).strip() if m else None


# ── borrower ─────────────────────────────────────────────────────────────────

_HONORIFIC = re.compile(r"\b(mr|mrs|ms|smt|shri|sri|thiru|tmt|m/s|messrs|dr)\b\.?", re.IGNORECASE)


def borrower_key(name: str | None) -> str:
    return normalize(_HONORIFIC.sub(" ", name or ""))


def borrower_matches(a: str | None, b: str | None) -> bool:
    x, y = borrower_key(a), borrower_key(b)
    if not x or not y:
        return False
    if x == y:
        return True
    try:
        from rapidfuzz import fuzz
    except ImportError:                 # pipeline dependency; without it only exact names match
        return False
    return fuzz.token_set_ratio(x, y) >= BORROWER_MATCH_MIN_SCORE


# ── building candidates ──────────────────────────────────────────────────────


def candidate_from_row(row: dict, *, doc_shas: Iterable[str] = ()) -> Candidate:
    """From a ``data/listings/<source>.jsonl`` row. ``doc_shas`` are the
    SHA-256s of the files the harvest downloaded for it, if the caller has
    hashed them."""
    text = " ".join(t for t in (row.get("title"), row.get("description")) if t)
    return Candidate(
        auction_id=str(row.get("auction_id") or ""),
        source=str(row.get("source") or ""),
        bank=row.get("bank_name") or "",
        reserve_price_num=row.get("reserve_price_num"),
        auction_start_dt=row.get("auction_start_dt"),
        borrower=row.get("borrower_name") or "",
        boundaries=extract_boundaries(text),
        identifiers=extract_identifiers(text),
        doc_shas={s for s in doc_shas if s},
    )


def candidate_from_graph(rec: dict) -> Candidate:
    """From one record of the gap report's fetch: ``auction_id``, ``source``,
    ``bank``, ``reserve_price_num``, ``auction_start_dt``, ``borrower``,
    ``boundaries`` (``{side: text}``), ``identifiers`` (``[[kind, value_norm]]``),
    ``doc_shas``. Boundary text is normalised here; identifier kinds are
    collapsed to families."""
    bounds = {s: normalize(v) for s, v in (rec.get("boundaries") or {}).items() if v}
    idents = set()
    for kind, value in rec.get("identifiers") or ():
        fam = identifier_family(kind)
        val = normalize_identifier_value(value)
        if fam and len(val.replace("/", "")) >= 2:
            idents.add((fam, val))
    return Candidate(
        auction_id=str(rec.get("auction_id") or ""),
        source=str(rec.get("source") or "eauctionsindia"),
        bank=rec.get("bank") or "",
        reserve_price_num=rec.get("reserve_price_num"),
        auction_start_dt=rec.get("auction_start_dt"),
        borrower=rec.get("borrower") or "",
        boundaries={s: v for s, v in bounds.items() if v},
        identifiers=idents,
        doc_shas={s for s in (rec.get("doc_shas") or ()) if s},
    )


# ── the matcher ──────────────────────────────────────────────────────────────

METHODS = ("notice_bytes", "boundaries", "identifier", "borrower", "bucket_only")
_ORDER = {m: i for i, m in enumerate(METHODS)}

#: Identifier families that name one unit rather than the land a batch shares.
UNIT_FAMILIES = frozenset({"villa", "flat", "plot", "door"})

#: Unit families whose disagreement vetoes a pair. Not door: a door number is
#: usually the building's address, shared by every flat in it.
VETO_FAMILIES = frozenset({"villa", "flat", "plot"})


@dataclass(frozen=True)
class Pair:
    a_id: str
    b_id: str
    a_source: str
    b_source: str
    method: str
    confidence: str
    evidence: str = ""


@dataclass(frozen=True)
class Ambiguity:
    """A listing the evidence could not pin to one partner on another portal.

    ``tied``: several candidates survived every tier. ``contested``: it chose
    a partner another listing chose too. Either way nothing is written, and
    the gap report counts it as undecided rather than new."""
    auction_id: str
    source: str
    other_source: str
    candidates: tuple[str, ...]
    reason: str


@dataclass
class MatchResult:
    pairs: list[Pair]
    ambiguous: list[Ambiguity]


def price_verdict(a: Candidate, b: Candidate) -> str:
    """``agree`` (within tolerance), ``unknown`` (a side has no price — the
    portal writes 0 or nothing for "not published"), or a disagreement."""
    verdict, _ = compare_prices(a.reserve_price_num if a.has_price else None,
                                b.reserve_price_num if b.has_price else None)
    return verdict


def _is_short(value: str) -> bool:
    """``g1``, ``22``, ``s1``: common enough across a city to need corroboration."""
    return len(value.replace("/", "")) <= 2


def _unit_key(value: str) -> str:
    """A unit number for comparison across notations: ``f/1`` and ``f1`` → ``1``,
    ``b/510`` → ``510``, ``86/b`` → ``86b``; ``ff12`` stays."""
    key = re.sub(r"[^a-z0-9]", "", value.lower())
    if re.match(r"^[a-z]\d", key):
        key = key[1:]
    return key


def _units_disagree(a: Candidate, b: Candidate) -> bool:
    """True when both quote a villa / flat / plot number and none of them agree:
    two different units, whatever else matches."""
    for fam in VETO_FAMILIES:
        keys_a = {_unit_key(v) for f, v in a.identifiers if f == fam}
        keys_b = {_unit_key(v) for f, v in b.identifiers if f == fam}
        if keys_a and keys_b and not keys_a & keys_b:
            return True
    return False


def _unique_identifiers(side: list[Candidate]) -> set[tuple[str, str]]:
    """Identifiers held by exactly one listing on this side of a bucket."""
    counts = Counter(i for c in side for i in c.identifiers)
    return {i for i, n in counts.items() if n == 1}


def _twin_groups(side: list[Candidate]) -> list[list[Candidate]]:
    """Listings on one portal that are postings of the same unit — equal unit
    numbers (villa / flat / plot / door), reserve price and borrower — as one
    group, in first-seen order. A listing that quotes no unit number is its
    own group: without one there is nothing to say two postings are one unit."""
    out: list[list[Candidate]] = []
    by_key: dict[tuple, list[Candidate]] = {}
    for c in side:
        units = frozenset(i for i in c.identifiers if i[0] in UNIT_FAMILIES)
        if not units:
            out.append([c])
            continue
        key = (units, _round_reserve(c.reserve_price_num), borrower_key(c.borrower))
        if key not in by_key:
            by_key[key] = []
            out.append(by_key[key])
        by_key[key].append(c)
    return out


def _representative(group: list[Candidate]) -> Candidate:
    """The group as one candidate: the first member, carrying every member's
    identifiers and notice fingerprints."""
    first = group[0]
    if len(group) == 1:
        return first
    return replace(first,
                   identifiers=set().union(*(c.identifiers for c in group)),
                   doc_shas=set().union(*(c.doc_shas for c in group)))


def _notice_tier(a: Candidate, cands: list[Candidate], usable: set) -> dict[int, str]:
    out = {}
    for pos, b in enumerate(cands):
        shared = a.doc_shas & b.doc_shas
        if shared:
            out[pos] = f"same notice file sha256 {sorted(shared)[0][:12]}…"
    return out


def _boundary_tier(a: Candidate, cands: list[Candidate], usable: set) -> dict[int, str]:
    return {pos: "three or more boundary neighbours agree"
            for pos, b in enumerate(cands) if boundary_matches(a.boundaries, b.boundaries)}


def _identifier_tier(a: Candidate, cands: list[Candidate], usable: set) -> dict[int, str]:
    out = {}
    for pos, b in enumerate(cands):
        ids = a.identifiers & b.identifiers & usable
        corroborated = len(ids) >= 2 or borrower_matches(a.borrower, b.borrower)
        ids = {i for i in ids if corroborated or not _is_short(i[1])}
        if ids:
            fam, val = sorted(ids)[0]
            out[pos] = f"same {fam} number {val}"
    return out


def _borrower_tier(a: Candidate, cands: list[Candidate], usable: set) -> dict[int, str]:
    return {pos: f"borrower '{a.borrower.strip()}' ~ '{b.borrower.strip()}'"
            for pos, b in enumerate(cands) if borrower_matches(a.borrower, b.borrower)}


_TIERS = (("notice_bytes", _notice_tier), ("boundaries", _boundary_tier),
          ("identifier", _identifier_tier), ("borrower", _borrower_tier))


def _choose(a: Candidate, side_b: list[Candidate], usable: set) -> tuple | None:
    """Narrow ``side_b`` to ``a``'s partner.

    Returns ``("match", index, method, evidence)``, ``("tied", indexes)`` or
    ``None``. Candidates whose price disagrees, or whose villa / flat / plot
    numbers all differ from ``a``'s (:func:`_units_disagree`), are dropped
    before any tier. A tier that hits every remaining candidate says nothing
    about which one ``a`` is and is skipped; one that hits some of them narrows."""
    idx = [i for i, b in enumerate(side_b) if price_verdict(a, b) in ("agree", "unknown")]
    idx = [i for i in idx if not _units_disagree(a, side_b[i])]
    if not idx:
        return None
    method, evidence, shared = None, {}, False
    for name, tier in _TIERS:
        hits = tier(a, [side_b[i] for i in idx], usable)
        if not hits:
            continue
        if len(hits) == len(idx) and len(idx) > 1:
            shared = True
            continue
        evidence = {idx[pos]: why for pos, why in hits.items()}
        idx = sorted(evidence)
        method = name
        if len(idx) == 1:
            break
    if method is None:
        if shared:
            return ("tied", idx)
        agreeing = [i for i in idx if price_verdict(a, side_b[i]) == "agree"]
        if len(agreeing) == 1:
            return ("match", agreeing[0], "bucket_only", "same bank, reserve price and auction day only")
        return ("tied", agreeing) if len(agreeing) > 1 else None
    if len(idx) == 1:
        return ("match", idx[0], method, evidence[idx[0]])
    return ("tied", idx)


def _assign(side_a: list[Candidate], side_b: list[Candidate], new_ids: set[str]) -> tuple[list[Pair], list[Ambiguity]]:
    """One portal against another inside one bucket, in rounds. Each round,
    every listing not yet paired chooses among the other side's unpaired
    listings, and a pair stands only when both choose each other; paired
    listings are taken and the round repeats while it adds a pair. One listing
    is one property, so a listing whose chosen partner was taken is not that
    partner: it chooses again from what is left, and if nothing is left it is
    simply unmatched. What the last round could not decide — a tie, or a
    choice the other listing did not return — is reported as an Ambiguity.

    Which identifiers are usable evidence is decided once, over the full
    sides: a survey number shared with a taken sibling stays shared."""
    groups_a, groups_b = _twin_groups(side_a), _twin_groups(side_b)
    reps_a = [_representative(g) for g in groups_a]
    reps_b = [_representative(g) for g in groups_b]
    usable = _unique_identifiers(reps_a) & _unique_identifiers(reps_b)

    def choices(free_x: list[int], reps_x: list[Candidate], free_y: list[int], reps_y: list[Candidate]) -> dict[int, tuple | None]:
        """Each free group's choice among the other side's free groups, with
        indexes mapped back to the full group lists."""
        pool = [reps_y[i] for i in free_y]
        out: dict[int, tuple | None] = {}
        for ix in free_x:
            got = _choose(reps_x[ix], pool, usable)
            if got is None:
                out[ix] = None
            elif got[0] == "tied":
                out[ix] = ("tied", [free_y[i] for i in got[1]])
            else:
                out[ix] = ("match", free_y[got[1]], got[2], got[3])
        return out

    pairs: list[Pair] = []
    ambiguous: list[Ambiguity] = []
    taken_a: set[int] = set()
    taken_b: set[int] = set()

    while True:
        free_a = [i for i in range(len(groups_a)) if i not in taken_a]
        free_b = [i for i in range(len(groups_b)) if i not in taken_b]
        choice_a = choices(free_a, reps_a, free_b, reps_b)
        choice_b = choices(free_b, reps_b, free_a, reps_a)
        added = False
        for ia in free_a:
            got = choice_a[ia]
            if got is None or got[0] != "match":
                continue
            _, ib, method_a, evidence_a = got
            got_b = choice_b[ib]
            if not (got_b is not None and got_b[0] == "match" and got_b[1] == ia):
                continue
            method_b, evidence_b = got_b[2], got_b[3]
            if _ORDER[method_b] < _ORDER[method_a]:
                method, evidence = method_b, evidence_b
            else:
                method, evidence = method_a, evidence_a
            for x in groups_a[ia]:
                for y in groups_b[ib]:
                    if x.auction_id in new_ids or y.auction_id in new_ids:
                        pairs.append(Pair(x.auction_id, y.auction_id, x.source, y.source,
                                          method, listing_confidence_for(method), evidence))
            taken_a.add(ia)
            taken_b.add(ib)
            added = True
        if not added:
            break

    def tied(group_x: list[Candidate], tied_groups: list[list[Candidate]]) -> list[Ambiguity]:
        others = tuple(sorted(y.auction_id for g in tied_groups for y in g))
        return [Ambiguity(x.auction_id, x.source, tied_groups[0][0].source, others, "tied")
                for x in group_x if x.auction_id in new_ids]

    def contested(group_x: list[Candidate], partner_group: list[Candidate]) -> list[Ambiguity]:
        others = tuple(sorted(y.auction_id for y in partner_group))
        return [Ambiguity(x.auction_id, x.source, partner_group[0].source, others, "contested")
                for x in group_x if x.auction_id in new_ids]

    # The last round added no pair, so its choices cover exactly the untaken groups.
    for ia, got in choice_a.items():
        if got is None:
            continue
        if got[0] == "tied":
            ambiguous += tied(groups_a[ia], [groups_b[i] for i in got[1]])
        else:
            ambiguous += contested(groups_a[ia], groups_b[got[1]])

    for ib, got in choice_b.items():
        if got is None:
            continue
        if got[0] == "tied":
            ambiguous += tied(groups_b[ib], [groups_a[i] for i in got[1]])
        else:
            ambiguous += contested(groups_b[ib], groups_a[got[1]])

    return pairs, ambiguous


def match_listings(incoming: Iterable[Candidate], existing: Iterable[Candidate]) -> MatchResult:
    """Every cross-source pair the evidence can pin one-to-one, and every
    listing it could not. ``incoming`` is compared against ``existing`` and
    against itself (two new portals can both carry an auction the graph has
    never seen); ``existing`` is never compared with itself — that is
    ``link_reauctions``' job. A listing present on both sides (a loaded
    harvest row) is taken once, from ``incoming``."""
    seen: set[str] = set()
    members_by_bucket: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
    new_ids: set[str] = set()
    for is_new, pool in ((True, incoming), (False, existing)):
        for c in pool:
            if not c.auction_id or c.auction_id in seen:
                continue
            seen.add(c.auction_id)
            if is_new:
                new_ids.add(c.auction_id)
            if c.bucket_key is not None:
                members_by_bucket[c.bucket_key].append(c)

    pairs: list[Pair] = []
    ambiguous: list[Ambiguity] = []
    for key in sorted(members_by_bucket):
        members = members_by_bucket[key]
        sources = sorted({c.source for c in members})
        for i, source_a in enumerate(sources):
            for source_b in sources[i + 1:]:
                side_a = [c for c in members if c.source == source_a]
                side_b = [c for c in members if c.source == source_b]
                if not any(c.auction_id in new_ids for c in side_a + side_b):
                    continue
                got_pairs, got_ambiguous = _assign(side_a, side_b, new_ids)
                pairs += got_pairs
                ambiguous += got_ambiguous
    pairs.sort(key=lambda p: (_ORDER[p.method], p.a_id, p.b_id))
    ambiguous.sort(key=lambda x: (x.auction_id, x.other_source))
    return MatchResult(pairs, ambiguous)


def find_same_listing_pairs(incoming: Iterable[Candidate], existing: Iterable[Candidate]) -> list[Pair]:
    """The pairs of :func:`match_listings`, strongest first."""
    return match_listings(incoming, existing).pairs
