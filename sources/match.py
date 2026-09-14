"""Cross-portal matching: is this incoming listing an auction we already hold?

Pure functions over dicts — no network, no graph. The callers
(``scripts/gap_report.py``, ``scripts/link_listings.py``) fetch what the graph
knows and hand it in beside the harvested rows.

Two listings are compared only when they come from different sources, name the
same bank (``pipeline.entity_resolution.org_key``) and the same auction day. The
*subject* is the listing from the better-ranked source (``sources.base.SOURCE_RANK``).
Against each other source (spec: docs/superpowers/specs/2026-09-15-portal-match-governance-design.md):

    exactly one listing agrees on reserve price (to the rupee) and borrower   four_fields  CONFIRMED
    several agree, and one villa/flat/plot/door number picks one of them      unit_number  CONFIRMED
    a person confirmed it                                                     decision     CONFIRMED
    anything partial — a batch, price only, borrower only, a split,
    disagreeing unit numbers, two subjects on one listing                     review       PENDING

Identical postings of one unit on one source count as one candidate.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Iterable

from pipeline.entity_resolution import normalize, org_key
from pipeline.lot_resolution import BORROWER_MATCH_MIN_SCORE, _round_reserve
from pipeline.match_confidence import listing_confidence_for
from sources.base import SOURCE_RANK

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
    #: Display only — what a reviewer reads. The rule never looks at it.
    info: dict = field(default_factory=dict)

    @property
    def bank_key(self) -> str:
        return org_key(self.bank or "")

    @property
    def day(self) -> str | None:
        return day_of(self.auction_start_dt)

    @property
    def bucket_key(self) -> tuple[str, str] | None:
        """Bank + day: the only listings the rule ever compares."""
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
        info={"title": row.get("title"), "description": row.get("description"), "city": row.get("city"),
              "district": row.get("district"), "url": row.get("source_url") or row.get("url"),
              "emd": row.get("emd_num"), "public_url": None},
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
        info={"title": rec.get("title"), "description": rec.get("description"), "city": rec.get("city"),
              "district": rec.get("district"), "url": rec.get("url"), "emd": rec.get("emd_num"),
              "public_url": rec.get("public_url")},
    )


# ── the matcher ──────────────────────────────────────────────────────────────

METHODS = ("decision", "four_fields", "unit_number", "review")
_ORDER = {m: i for i, m in enumerate(METHODS)}

#: Why a subject waits for a person (``Ambiguity.reason``).
REVIEW_REASONS = ("batch", "units_disagree", "price_only", "borrower_only", "split", "contested")

#: Identifier families that name one unit rather than the land a batch shares.
UNIT_FAMILIES = frozenset({"villa", "flat", "plot", "door"})

#: Unit families whose disagreement sends a pair to review. Not door: a door
#: number is usually the building's address, shared by every flat in it.
VETO_FAMILIES = frozenset({"villa", "flat", "plot"})

#: Rank for a source ``SOURCE_RANK`` does not list: after every known one.
_UNKNOWN_RANK = 99


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
    """A subject listing waiting for a person. ``reason`` is one of
    :data:`REVIEW_REASONS`; ``candidates`` are the other source's listings the
    person decides between."""
    auction_id: str
    source: str
    other_source: str
    candidates: tuple[str, ...]
    reason: str


@dataclass
class MatchResult:
    pairs: list[Pair]
    ambiguous: list[Ambiguity]


def price_equal(a: Candidate, b: Candidate) -> bool:
    """Both publish a reserve price and they agree to the rupee."""
    if not (a.has_price and b.has_price):
        return False
    return abs(float(a.reserve_price_num) - float(b.reserve_price_num)) < 1


def _unit_key(value: str) -> str:
    """A unit number for comparison across notations: ``f/1`` and ``f1`` → ``1``,
    ``b/510`` → ``510``, ``86/b`` → ``86b``; ``ff12`` stays."""
    key = re.sub(r"[^a-z0-9]", "", value.lower())
    if re.match(r"^[a-z]\d", key):
        key = key[1:]
    return key


def _unit_keys(c: Candidate, families: frozenset[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for fam, value in c.identifiers:
        if fam in families:
            out[fam].add(_unit_key(value))
    return out


def _units_disagree(a: Candidate, b: Candidate) -> bool:
    """Both quote a villa / flat / plot number and none of them agree."""
    ka, kb = _unit_keys(a, VETO_FAMILIES), _unit_keys(b, VETO_FAMILIES)
    return any(ka[f] and kb[f] and not ka[f] & kb[f] for f in VETO_FAMILIES)


def _shared_unit(a: Candidate, b: Candidate) -> tuple[str, str] | None:
    """The first unit number (family, key) both quote, or None."""
    ka, kb = _unit_keys(a, UNIT_FAMILIES), _unit_keys(b, UNIT_FAMILIES)
    for fam in sorted(UNIT_FAMILIES):
        common = ka[fam] & kb[fam]
        if common:
            return fam, sorted(common)[0]
    return None


def _twin_groups(side: list[Candidate]) -> list[list[Candidate]]:
    """Listings on one source that are postings of the same unit — equal unit
    numbers (villa / flat / plot / door), reserve price and borrower — as one
    group, in first-seen order. A listing that quotes no unit number is its
    own group."""
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


def _judge(subject: Candidate, reps: list[Candidate]) -> tuple | None:
    """The rule for one subject against one other source's candidates.

    Returns ``("link", index, method, evidence)``, ``("review", reason,
    indexes)`` or ``None`` (nothing agrees: the subject is new)."""
    price = [i for i, c in enumerate(reps) if price_equal(subject, c)]
    borrower = [i for i, c in enumerate(reps) if borrower_matches(subject.borrower, c.borrower)]
    if not price and not borrower:
        return None
    full = [i for i in price if i in borrower]
    if len(full) == 1:
        if _units_disagree(subject, reps[full[0]]):
            return ("review", "units_disagree", full)
        return ("link", full[0], "four_fields", "same bank, auction day, reserve price and borrower")
    if len(full) > 1:
        hits = []
        for i in full:
            unit = _shared_unit(subject, reps[i])
            if unit and not _units_disagree(subject, reps[i]):
                hits.append((i, unit))
        if len(hits) == 1:
            i, (fam, value) = hits[0]
            return ("link", i, "unit_number",
                    f"same bank, auction day, reserve price and borrower; same {fam} number {value}")
        return ("review", "batch", full)
    if price and not borrower:
        return ("review", "price_only", price)
    if borrower and not price:
        return ("review", "borrower_only", borrower)
    return ("review", "split", sorted(set(price) | set(borrower)))


def _pairs(group_s: list[Candidate], targets: list[list[Candidate]], method: str, evidence: str,
           new_ids: set[str]) -> list[Pair]:
    return [Pair(x.auction_id, y.auction_id, x.source, y.source, method, listing_confidence_for(method), evidence)
            for x in group_s for g in targets for y in g
            if x.auction_id in new_ids or y.auction_id in new_ids]


def _assign(side_s: list[Candidate], side_o: list[Candidate], new_ids: set[str]) -> tuple[list[Pair], list[Ambiguity]]:
    """Every subject group of one source against one other source, in one bucket.
    Two subjects that would link the same candidate group are both contested."""
    groups_s, groups_o = _twin_groups(side_s), _twin_groups(side_o)
    reps_o = [_representative(g) for g in groups_o]
    verdicts: dict[int, tuple | None] = {si: _judge(_representative(g), reps_o) for si, g in enumerate(groups_s)}

    linkers: dict[int, list[int]] = defaultdict(list)
    for si, v in verdicts.items():
        if v is not None and v[0] == "link":
            linkers[v[1]].append(si)
    for oi, sis in linkers.items():
        if len(sis) > 1:
            for si in sis:
                verdicts[si] = ("review", "contested", [oi])

    pairs: list[Pair] = []
    ambiguous: list[Ambiguity] = []
    for si, v in sorted(verdicts.items()):
        if v is None:
            continue
        if v[0] == "link":
            _, oi, method, evidence = v
            pairs += _pairs(groups_s[si], [groups_o[oi]], method, evidence, new_ids)
            continue
        _, reason, ois = v
        targets = [groups_o[oi] for oi in ois]
        pairs += _pairs(groups_s[si], targets, "review", reason, new_ids)
        others = tuple(sorted(y.auction_id for g in targets for y in g))
        ambiguous += [Ambiguity(x.auction_id, x.source, targets[0][0].source, others, reason)
                      for x in groups_s[si] if x.auction_id in new_ids]
    return pairs, ambiguous


def _rank(source: str) -> tuple[int, str]:
    return (SOURCE_RANK.get(source, _UNKNOWN_RANK), source)


def match_listings(incoming: Iterable[Candidate], existing: Iterable[Candidate]) -> MatchResult:
    """Every link the rule confirms, every pair waiting for a person, and one
    ``Ambiguity`` per waiting subject. ``incoming`` is compared against
    ``existing`` and against itself; ``existing`` is never compared with itself
    — that is ``link_reauctions``' job. A listing present on both sides (a
    loaded harvest row) is taken once, from ``incoming``."""
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
        sources = sorted({c.source for c in members}, key=_rank)
        for i, source_s in enumerate(sources):
            for source_o in sources[i + 1:]:
                side_s = [c for c in members if c.source == source_s]
                side_o = [c for c in members if c.source == source_o]
                if not any(c.auction_id in new_ids for c in side_s + side_o):
                    continue
                got_pairs, got_ambiguous = _assign(side_s, side_o, new_ids)
                pairs += got_pairs
                ambiguous += got_ambiguous
    pairs.sort(key=lambda p: (_ORDER[p.method], p.a_id, p.b_id))
    ambiguous.sort(key=lambda x: (x.auction_id, x.other_source))
    return MatchResult(pairs, ambiguous)


def find_same_listing_pairs(incoming: Iterable[Candidate], existing: Iterable[Candidate]) -> list[Pair]:
    """The pairs of :func:`match_listings`."""
    return match_listings(incoming, existing).pairs
