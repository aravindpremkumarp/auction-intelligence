"""Cross-portal matching: is this incoming listing an auction we already hold?

Pure functions over dicts — no network, no graph. The caller (today
``scripts/gap_report.py``, later ``scripts/link_listings.py``) fetches what
the graph knows and hands it in beside the harvested rows.

Two listings are only ever compared across *different* sources. A portal
never lists one auction twice under two ids, but it does list a same-day
batch sale as several listings that share bank, borrower, day and often
reserve price (BAANKNET ``bn-351743`` / ``bn-351740``), which is exactly what
the bucket alone cannot tell apart. Within a bucket the evidence is graded,
strongest first:

    notice_bytes   the same sale-notice file on both sides       CONFIRMED
    boundaries     three of four neighbours agree                CONFIRMED
    identifier     the same survey / door / plot / flat number   PROBABLE
    borrower       the same party (token_set_ratio >= 90)        PROBABLE
    bucket_only    bank + reserve + day and nothing more         INFERRED

The bucket is canonical bank (``pipeline.entity_resolution.org_key``),
auction calendar day, and reserve price within
``pipeline.price_agreement.TOLERANCE_PCT`` — not a rounded-price key, so a
figure quoted as 45,60,000 on one portal and 45,60,000.00 on another, or
rounded to the thousand in a newspaper, still lands in the same bucket.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
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
           "plot": "plot", "flat": "flat"}

_IDENT = re.compile(
    r"\b(?P<kind>"
    r"(?:old\s+|new\s+|t\.?\s*s\.?\s*|r\.?\s*s\.?\s*|re-?survey\s*|survey\s+|s\.?\s*)no\.?s?"
    r"|(?:old\s+|new\s+)?(?:door|d\.?)\s*no\.?s?"
    r"|plot\s+no\.?s?"
    r"|flat\s+no\.?s?"
    r")\s*[:.\-]?\s*"
    r"(?P<value>(?:\d+[A-Za-z]?|[A-Za-z]\d+)(?:\s*(?:/|by|-)\s*\d*[A-Za-z]?\d*)*)",
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
        if "plot" in kind:
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


@dataclass(frozen=True)
class Pair:
    a_id: str
    b_id: str
    a_source: str
    b_source: str
    method: str
    confidence: str
    evidence: str = ""


def evidence_for(a: Candidate, b: Candidate) -> tuple[str, str] | None:
    """The strongest method that holds for two candidates already known to
    share a bucket, with a one-line human reason; ``None`` when nothing beyond
    the bucket holds (the caller then reports ``bucket_only``)."""
    shared = a.doc_shas & b.doc_shas
    if shared:
        return "notice_bytes", f"same notice file sha256 {sorted(shared)[0][:12]}…"
    if boundary_matches(a.boundaries, b.boundaries):
        return "boundaries", "three or more boundary neighbours agree"
    ids = a.identifiers & b.identifiers
    if ids:
        fam, val = sorted(ids)[0]
        return "identifier", f"same {fam} number {val}"
    if borrower_matches(a.borrower, b.borrower):
        return "borrower", f"borrower '{a.borrower.strip()}' ~ '{b.borrower.strip()}'"
    return None


def price_verdict(a: Candidate, b: Candidate) -> str:
    """``agree`` (within tolerance), ``unknown`` (a side has no price — the
    portal writes 0 or nothing for "not published"), or a disagreement."""
    verdict, _ = compare_prices(a.reserve_price_num if a.has_price else None,
                                b.reserve_price_num if b.has_price else None)
    return verdict


def find_same_listing_pairs(incoming: Iterable[Candidate], existing: Iterable[Candidate]) -> list[Pair]:
    """Every cross-source pair that shares a bucket, graded. ``incoming`` is
    compared against ``existing`` and against itself (two new portals can
    both carry an auction the graph has never seen); ``existing`` is never
    compared with itself — that is ``link_reauctions``' job.

    Same-source pairs are never emitted, whatever the evidence. A pair where
    one side has no reserve price needs evidence beyond the bucket (the 2026
    graph holds hundreds of listings whose portal price was "not published");
    a pair whose prices disagree is never emitted.
    """
    inc = [c for c in incoming if c.auction_id]
    ext = [c for c in existing if c.auction_id]
    buckets: dict[tuple[str, str], list[tuple[bool, Candidate]]] = {}
    for is_new, pool in ((True, inc), (False, ext)):
        for c in pool:
            key = c.bucket_key
            if key is not None:
                buckets.setdefault(key, []).append((is_new, c))

    seen: set[tuple[str, str]] = set()
    pairs: list[Pair] = []
    for members in buckets.values():
        for i, (new_i, a) in enumerate(members):
            for new_j, b in members[i + 1:]:
                if not (new_i or new_j):
                    continue
                if a.source == b.source:
                    continue
                verdict = price_verdict(a, b)
                if verdict not in ("agree", "unknown"):
                    continue
                key = tuple(sorted((a.auction_id, b.auction_id)))
                if key in seen:
                    continue
                seen.add(key)
                found = evidence_for(a, b)
                if found is None and verdict != "agree":
                    continue
                method, why = found if found else ("bucket_only", "same bank, reserve price and auction day only")
                pairs.append(Pair(a.auction_id, b.auction_id, a.source, b.source,
                                  method, listing_confidence_for(method), why))
    order = {m: i for i, m in enumerate(METHODS)}
    pairs.sort(key=lambda p: (order[p.method], p.a_id, p.b_id))
    return pairs
