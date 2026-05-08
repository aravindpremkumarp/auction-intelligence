"""
scripts/link_reauctions.py
--------------------------
Detect re-auctions (same property auctioned more than once) and materialise
them as `:SAME_PROPERTY_AS` relationships in Neo4j.

Rules:

  1. Borrower+location+description rule — same Borrower, Bank, and
     (normalised) Area, AND description Jaccard similarity ≥ threshold.
     If total_area is available on both sides it must also agree
     within ±10% (normalised to sq ft). Missing description on either
     side ⇒ no match (conservative default).

Both rules additionally require the two auctions to fall on DIFFERENT
calendar days. Same-day matches are batch sales (sibling parcels
auctioned together), not re-auctions, no matter how identical the
descriptions look. This same-day filter is also re-applied to the
transitive cluster-expansion output so sibling parcels don't leak in
via a different-day neighbour.

Before computing description Jaccard the matcher subtracts tokens that
appear in >=50% of a candidate group's descriptions — registration
district / taluk / sub-registration boilerplate that inflates Jaccard
between genuinely distinct parcels owned by the same borrower in the
same administrative area.

Clusters are transitive: if A matches B and B matches C, all three are
linked. Each pair in a cluster is connected with a bidirectional MERGE.

Run:
    python -m scripts.link_reauctions            # detect + write
    python -m scripts.link_reauctions --dry-run  # detect only, print stats
    python -m scripts.link_reauctions --rebuild  # drop existing edges first
    python -m scripts.link_reauctions --debug    # diagnostic coverage view
    python -m scripts.link_reauctions --sim-threshold 0.5  # tune matcher
"""
from __future__ import annotations

import argparse
import re
import time
from collections import Counter, defaultdict
from typing import Iterable

from neo4j import GraphDatabase

from pipeline.config import (
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD,
    NEO4J_DATABASE, NEO4J_BATCH_SIZE,
)


# ── Total-area parsing ───────────────────────────────────────────────────────

# Multipliers to convert various area units into sq ft.
_UNIT_TO_SQFT: dict[str, float] = {
    "sqft": 1.0, "sq ft": 1.0, "sq. ft": 1.0, "sq.ft": 1.0, "sft": 1.0,
    "ft2": 1.0, "ft^2": 1.0, "square feet": 1.0, "square foot": 1.0,
    "sqm": 10.7639, "sq m": 10.7639, "sq. m": 10.7639, "m2": 10.7639,
    "square meter": 10.7639, "square metre": 10.7639, "square meters": 10.7639,
    "sqyd": 9.0, "sq yd": 9.0, "sq. yd": 9.0, "yd2": 9.0,
    "square yard": 9.0, "square yards": 9.0,
    "cent": 435.6,
    "acre": 43560.0, "acres": 43560.0,
    "hectare": 107639.0, "hectares": 107639.0,
    "ground": 2400.0,  # Tamil Nadu convention
}

_NUMBER_RE = re.compile(r"([0-9][0-9,]*\.?[0-9]*)")


def parse_total_area_sqft(raw: str | None) -> float | None:
    """Parse a free-text area string into sq ft. Returns None if unparseable.

    Handles: "1295 Sq Ft", "120 sq m", "0.5 acres", "3 cents", "1,250 sqft".
    Strings without a recognised unit fall back to assuming sq ft if the
    number is >= 50 (anything smaller is likely in a larger unit we can't
    identify — reject rather than mismatch).
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    m = _NUMBER_RE.search(s)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if num <= 0:
        return None

    # Pick the longest matching unit suffix (so "sq m" wins over "m").
    tail = s[m.end():].strip()
    best: tuple[int, float] | None = None
    for unit, mult in _UNIT_TO_SQFT.items():
        if unit in tail:
            if best is None or len(unit) > best[0]:
                best = (len(unit), mult)
    if best is not None:
        return num * best[1]
    # Bare number: only trust if it's a plausible sq-ft figure.
    if num >= 50:
        return num
    return None


def areas_agree(a: float | None, b: float | None, tolerance: float = 0.10) -> bool:
    if a is None or b is None:
        return False
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / max(a, b) <= tolerance


# ── Area-name normalisation ──────────────────────────────────────────────────

_AREA_SUFFIXES = (" taluk", " district", " village", " town", " panchayat")


def normalize_area(raw: str | None) -> str | None:
    """Normalise an area name so OCR/casing variants collide on one key.

    Handles "Vedasandur.", "vedasandur", "Poonamallee Taluk", etc.
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    # Strip trailing punctuation (dot, comma, semicolon).
    s = re.sub(r"[.,;:\s]+$", "", s)
    # Drop administrative suffixes; apply once, longest first.
    for suffix in sorted(_AREA_SUFFIXES, key=len, reverse=True):
        if s.endswith(suffix):
            s = s[: -len(suffix)].rstrip()
            break
    # Collapse internal whitespace runs.
    s = re.sub(r"\s+", " ", s)
    return s or None


# ── Description token Jaccard similarity ─────────────────────────────────────

# Short, high-frequency English + sale-notice boilerplate that appears in
# almost every description and so carries no discriminating signal.
_DESC_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "into", "upon",
    "are", "was", "were", "has", "have", "had", "will", "shall", "been",
    "but", "not", "any", "all", "its", "his", "her", "their", "there",
    "also", "may", "per", "out", "who", "whom", "which", "where", "when",
    "sale", "auction", "notice", "property", "properties", "bank",
    "bidder", "bidders", "bid", "bids", "borrower", "borrowers", "said",
    "schedule", "scheduled", "lot", "item", "number", "rupees", "rs",
    "inr", "crore", "lakh", "lakhs", "crores", "thousand", "only",
    "limited", "ltd", "pvt", "private", "mortgage", "mortgaged",
    "secured", "creditor", "reserve", "price", "emd",
})

_WORD_RE = re.compile(r"[a-z0-9]+")


def tokenize_description(raw: str | None) -> set[str] | None:
    """Return a set of discriminative tokens for Jaccard comparison, or
    None if the description is empty/unusable.

    Drops short alphabetic tokens (noise like 'of', 'at', 'by'), stopwords,
    but keeps purely numeric tokens regardless of length — short numbers
    are door/survey/lot identifiers that carry most of the signal.
    """
    if not raw or not isinstance(raw, str):
        return None
    tokens = set()
    for tok in _WORD_RE.findall(raw.lower()):
        if tok in _DESC_STOPWORDS:
            continue
        if tok.isdigit():
            tokens.add(tok)
            continue
        if len(tok) < 3:
            continue
        tokens.add(tok)
    return tokens or None


def jaccard(a: set[str] | None, b: set[str] | None) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


# ── Parcel-identifier extraction ─────────────────────────────────────────────

# Strict: number tokens immediately following a standard parcel-prefix
# ("Re Sy No", "Survey No", "Sf No", "T.S. No", "Plot No", "Door No", "D No",
# "Site No"). These are the most reliable per-parcel identifiers in Tamil
# Nadu sale notices.
#
# `\b` at the start of each prefix prevents matching inside a larger word
# (e.g. without it, `d\s*no` would match `d No` inside `"...an[d No]rth
# by..."` and capture `rth`). The `[\.\s]+` after `no` requires a real
# separator before the captured token, so `NorthXYZ` can't contribute.
_STRICT_PARCEL_RE = re.compile(
    r"\b(?:re\.?\s*sy\.?\s*no|sy\.?\s*no|survey\s*no|s\.\s*no|sf\s*no"
    r"|t\.?\s*s\.?\s*no|plot\s*no|door\s*no|d\.?\s*no|site\s*no)"
    r"[\.\s]+"
    r"([A-Za-z0-9][\w/\-\.]*)",
    re.IGNORECASE,
)

# Loose: any slash/dash-separated alphanumeric token that looks like
# "N/M" or "N/M-X" — catches survey-number-shaped tokens even when the
# prefix is mangled or missing.
_LOOSE_PARCEL_RE = re.compile(
    r"\b(\d{1,5}[/\-][\w\-]{1,8}(?:[/\-][\w\-]{1,8})?)\b"
)

# Looks like a DD/MM/YYYY or similar date — exclude from parcel IDs.
_DATE_LIKE_RE = re.compile(r"(?:^|[/\-])(19|20)\d{2}$")


def _normalize_parcel_id(raw: str) -> str | None:
    t = re.sub(r"[^\w/\-]", "", raw.lower()).strip("./- ")
    if not t or len(t) < 2:
        return None
    if _DATE_LIKE_RE.search(t):
        return None
    return t


def strict_parcel_ids(desc: str | None) -> set[str]:
    if not desc or not isinstance(desc, str):
        return set()
    out: set[str] = set()
    for raw in _STRICT_PARCEL_RE.findall(desc):
        t = _normalize_parcel_id(raw)
        if t:
            out.add(t)
    return out


def loose_parcel_ids(desc: str | None) -> set[str]:
    if not desc or not isinstance(desc, str):
        return set()
    out: set[str] = set()
    for raw in _LOOSE_PARCEL_RE.findall(desc):
        t = _normalize_parcel_id(raw)
        if t:
            out.add(t)
    return out


# ── Description-embedded area parsing ───────────────────────────────────────

# Matches "1295 sq ft", "20.50 cents", "13.36 ares", "0.5 acre", etc.
_DESC_AREA_RE = re.compile(
    r"([0-9][0-9,]*\.?[0-9]*)\s*"
    r"(square\s*feet|square\s*foot|sq\.?\s*ft|sqft|sft|ft2"
    r"|square\s*meters?|square\s*metres?|sq\.?\s*m|sqm|m2"
    r"|square\s*yards?|sq\.?\s*yd|sqyd|yd2"
    r"|cents?|ares?|acres?|hectares?|grounds?)",
    re.IGNORECASE,
)


def parse_desc_areas(desc: str | None) -> set[float]:
    """Extract every `<number> <unit>` area mention from a description
    and normalise to sq ft. Useful when `total_area` is null on the node
    but the description itself lists parcel sizes ("20.50 cents", etc.).
    """
    if not desc or not isinstance(desc, str):
        return set()
    out: set[float] = set()
    for num_s, unit_raw in _DESC_AREA_RE.findall(desc):
        try:
            num = float(num_s.replace(",", ""))
        except ValueError:
            continue
        if num <= 0:
            continue
        unit = re.sub(r"[\s\.]", "", unit_raw.lower())
        mult = {
            "squarefeet": 1.0, "squarefoot": 1.0, "sqft": 1.0, "sft": 1.0, "ft2": 1.0,
            "squaremeter": 10.7639, "squaremeters": 10.7639,
            "squaremetre": 10.7639, "squaremetres": 10.7639,
            "sqm": 10.7639, "m2": 10.7639,
            "squareyard": 9.0, "squareyards": 9.0, "sqyd": 9.0, "yd2": 9.0,
            "cent": 435.6, "cents": 435.6,
            "are": 1076.39, "ares": 1076.39,
            "acre": 43560.0, "acres": 43560.0,
            "hectare": 107639.0, "hectares": 107639.0,
            "ground": 2400.0, "grounds": 2400.0,
        }.get(unit)
        if mult is not None:
            out.add(round(num * mult, 1))
    return out


def desc_areas_agree(a: set[float], b: set[float], tolerance: float = 0.10) -> bool:
    if not a or not b:
        return False
    return any(areas_agree(pa, pb, tolerance) for pa in a for pb in b)


# ── Union-find for transitive clustering ─────────────────────────────────────

class _UnionFind:
    def __init__(self, keys: Iterable[str]) -> None:
        self._parent: dict[str, str] = {k: k for k in keys}

    def find(self, x: str) -> str:
        p = self._parent
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for k in self._parent:
            out[self.find(k)].append(k)
        return out


# ── Matching ─────────────────────────────────────────────────────────────────

def _norm(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip()
    return s.lower() if s else None


def _is_same_auction_day(dt_a: str | None, dt_b: str | None) -> bool:
    """True iff both ISO-8601 datetimes fall on the same calendar day.

    Re-auctions happen on different dates. Two auctions sharing the same
    start day are a batch sale (two distinct parcels sold together), not
    a re-auction — even if every other field matches.
    """
    if not dt_a or not dt_b:
        return False
    if not isinstance(dt_a, str) or not isinstance(dt_b, str):
        return False
    return dt_a[:10] == dt_b[:10] and len(dt_a) >= 10 and len(dt_b) >= 10


# Threshold above which description Jaccard similarity counts as a match.
# Above DESC_SIM_HIGH, bump confidence to "high"; between threshold and
# DESC_SIM_HIGH, "medium".
DEFAULT_SIM_THRESHOLD = 0.4
DESC_SIM_HIGH = 0.6

# Fraction of a candidate group's descriptions a token must appear in for it
# to be treated as boilerplate and subtracted before computing Jaccard.
# Applied only when the group is large enough to tell boilerplate from
# distinctive content — in a 2-auction group every shared token is "100%
# common" but obviously carries signal.
BOILERPLATE_GROUP_FRACTION = 0.75
# Only subtract boilerplate for groups big enough that "token appearing
# in 75% of members" genuinely distinguishes shared scaffolding from
# shared signal. 4 is the smallest group where the cutoff is meaningful.
BOILERPLATE_MIN_GROUP_SIZE = 4


def group_boilerplate_tokens(
    token_sets: list[set[str] | None],
    fraction: float = BOILERPLATE_GROUP_FRACTION,
    min_group_size: int = BOILERPLATE_MIN_GROUP_SIZE,
) -> set[str]:
    """Tokens appearing in `fraction` or more of the non-empty members of a
    candidate group, but only for groups of at least `min_group_size`.

    These are the shared scaffolding (Registration District, Taluk, etc.)
    that inflates Jaccard for unrelated parcels from the same administrative
    area. For small groups (2 members) we can't tell boilerplate from
    signal, so we return an empty set and let the raw Jaccard decide.
    """
    usable = [t for t in token_sets if t]
    if len(usable) < min_group_size:
        return set()
    counts: dict[str, int] = defaultdict(int)
    for s in usable:
        for tok in s:
            counts[tok] += 1
    cutoff = max(2, int(round(len(usable) * fraction)))
    return {tok for tok, n in counts.items() if n >= cutoff}


def find_reauction_pairs(
    auctions: list[dict],
    sim_threshold: float = DEFAULT_SIM_THRESHOLD,
) -> list[tuple[str, str, str, str]]:
    """Return (a_id, b_id, match_reason, confidence) pairs.

    `auctions` is a list of dicts with keys:
        auction_id, borrower, bank, city, area, total_area, description,
        auction_start_dt
    """
    pairs: list[tuple[str, str, str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()

    def _add(a: str, b: str, reason: str, confidence: str) -> None:
        if a == b:
            return
        key = (a, b) if a < b else (b, a)
        if key in seen_pairs:
            return
        seen_pairs.add(key)
        pairs.append((*key, reason, confidence))

    # Pre-compute description-derived token/ID sets once per auction.
    desc_tokens: dict[str, set[str] | None] = {}
    strict_ids: dict[str, set[str]] = {}
    loose_ids: dict[str, set[str]] = {}
    desc_areas: dict[str, set[float]] = {}
    for row in auctions:
        aid = row["auction_id"]
        desc = row.get("description")
        desc_tokens[aid] = tokenize_description(desc)
        strict_ids[aid] = strict_parcel_ids(desc)
        loose_ids[aid] = loose_parcel_ids(desc)
        desc_areas[aid] = parse_desc_areas(desc)

    # Rule: borrower + bank + normalised-area, validated by description
    # Jaccard similarity. Candidates grouped first; every within-group pair
    # is then checked for description overlap and total_area agreement.
    by_bba: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in auctions:
        br = _norm(row.get("borrower"))
        bk = _norm(row.get("bank"))
        ar = normalize_area(row.get("area"))
        if not (br and bk and ar):
            continue
        by_bba[(br, bk, ar)].append(row)

    for bucket in by_bba.values():
        if len(bucket) < 2:
            continue
        # Identify boilerplate tokens local to this candidate group so
        # generic registration-district / taluk scaffolding doesn't inflate
        # Jaccard between genuinely distinct parcels owned by the same
        # borrower in the same area.
        bucket_tokens = [desc_tokens.get(a["auction_id"]) for a in bucket]
        boilerplate = group_boilerplate_tokens(bucket_tokens)

        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                a, b = bucket[i], bucket[j]
                # Two auctions on the same calendar day are a batch sale
                # (sibling parcels), not a re-auction of one property.
                if _is_same_auction_day(a.get("auction_start_dt"),
                                        b.get("auction_start_dt")):
                    continue
                a_id, b_id = a["auction_id"], b["auction_id"]
                tokens_a = desc_tokens.get(a_id)
                tokens_b = desc_tokens.get(b_id)
                if not tokens_a or not tokens_b:
                    # Conservative: missing description on either side → skip.
                    continue
                # Subtract group boilerplate before measuring similarity.
                discr_a = tokens_a - boilerplate
                discr_b = tokens_b - boilerplate
                sim = jaccard(discr_a, discr_b)
                if sim < sim_threshold:
                    continue
                # If both sides have parseable total_area, they must agree
                # within tolerance — catches same-borrower neighbouring
                # parcels that happen to share description boilerplate.
                area_a = parse_total_area_sqft(a.get("total_area"))
                area_b = parse_total_area_sqft(b.get("total_area"))
                if area_a is not None and area_b is not None and not areas_agree(area_a, area_b):
                    continue
                # Tiered parcel-identifier gate: sale-notice boilerplate
                # from the same borrower passes Jaccard easily, so require
                # an explicit same-parcel signal before declaring a match.
                sa, sb = strict_ids[a_id], strict_ids[b_id]
                if sa and sb:
                    if not (sa & sb):
                        continue
                else:
                    la, lb = loose_ids[a_id], loose_ids[b_id]
                    if la and lb:
                        if not (la & lb):
                            continue
                    else:
                        # Tier 3: neither side exposed a parcel ID. Only
                        # accept if both descriptions are very similar AND
                        # advertise the same parcel size.
                        if sim < DESC_SIM_HIGH:
                            continue
                        da, db = desc_areas[a_id], desc_areas[b_id]
                        if not desc_areas_agree(da, db):
                            continue
                confidence = "high" if sim >= DESC_SIM_HIGH else "medium"
                _add(a_id, b_id, "borrower_location_desc", confidence)

    return pairs


def expand_clusters(
    auctions: list[dict],
    pairs: list[tuple[str, str, str, str]],
) -> tuple[list[list[str]], list[tuple[str, str, str, str]]]:
    """Apply union-find to make clustering transitive, then emit every
    pair within each cluster with the best reason/confidence seen.

    Same-day pairs are dropped from the emission even when union-find
    merges their components via different-day neighbours — they're still
    batch-sale siblings, not re-auctions.
    """
    date_by_id = {a["auction_id"]: a.get("auction_start_dt") for a in auctions}

    uf = _UnionFind(a["auction_id"] for a in auctions)
    pair_meta: dict[tuple[str, str], tuple[str, str]] = {}
    for a, b, reason, conf in pairs:
        uf.union(a, b)
        key = (a, b) if a < b else (b, a)
        prev = pair_meta.get(key)
        if prev is None or _confidence_rank(conf) > _confidence_rank(prev[1]):
            pair_meta[key] = (reason, conf)

    groups = uf.groups()
    clusters = [g for g in groups.values() if len(g) > 1]

    expanded: list[tuple[str, str, str, str]] = []
    for group in clusters:
        # Pick the strongest reason among any pair in the cluster as the
        # per-cluster default for pairs we didn't match directly.
        best_reason, best_conf = "borrower_location", "medium"
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                key = (group[i], group[j]) if group[i] < group[j] else (group[j], group[i])
                rc = pair_meta.get(key)
                if rc and _confidence_rank(rc[1]) > _confidence_rank(best_conf):
                    best_reason, best_conf = rc
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                # Drop transitive same-day pairs — they're batch siblings,
                # not re-auctions, even if they share a cluster with
                # legitimate different-day pairs.
                if _is_same_auction_day(date_by_id.get(a), date_by_id.get(b)):
                    continue
                key = (a, b) if a < b else (b, a)
                reason, conf = pair_meta.get(key, (best_reason, best_conf))
                expanded.append((*key, reason, conf))
    return clusters, expanded


def _confidence_rank(c: str) -> int:
    return {"high": 2, "medium": 1}.get(c, 0)


# ── Neo4j IO ─────────────────────────────────────────────────────────────────

FETCH_CANDIDATES = """
MATCH (a:AuctionProperty)
OPTIONAL MATCH (a)-[:HAS_BORROWER]->(br:Borrower)
OPTIONAL MATCH (a)-[:CONDUCTED_BY]->(bk:Bank)
OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(c:City)
OPTIONAL MATCH (a)-[:LOCATED_IN_AREA]->(ar:Area)
RETURN a.auction_id       AS auction_id,
       a.total_area        AS total_area,
       coalesce(a.enriched_description, a.description) AS description,
       toString(a.auction_start_dt) AS auction_start_dt,
       br.name             AS borrower,
       bk.name             AS bank,
       c.name              AS city,
       ar.name             AS area
"""

DROP_EXISTING = """
MATCH ()-[r:SAME_PROPERTY_AS]->() DELETE r
"""

MERGE_PAIR = """
UNWIND $rows AS r
MATCH (a:AuctionProperty {auction_id: r.a_id})
MATCH (b:AuctionProperty {auction_id: r.b_id})
MERGE (a)-[ab:SAME_PROPERTY_AS]->(b)
  ON CREATE SET ab.match_reason = r.reason,
                ab.confidence   = r.confidence,
                ab.linked_at    = datetime()
  ON MATCH  SET ab.match_reason = r.reason,
                ab.confidence   = r.confidence,
                ab.linked_at    = datetime()
MERGE (b)-[ba:SAME_PROPERTY_AS]->(a)
  ON CREATE SET ba.match_reason = r.reason,
                ba.confidence   = r.confidence,
                ba.linked_at    = datetime()
  ON MATCH  SET ba.match_reason = r.reason,
                ba.confidence   = r.confidence,
                ba.linked_at    = datetime()
"""


def fetch_auctions(session) -> list[dict]:
    return [dict(r) for r in session.run(FETCH_CANDIDATES)]


def debug_diagnostics(auctions: list[dict]) -> None:
    """Print coverage stats + near-miss groups so we can see why matching
    isn't firing on real data. Meant for the --debug flag."""
    n = len(auctions)
    print(f"\n=== Field coverage ({n} auctions) ===")
    has_borrower = sum(1 for a in auctions if _norm(a.get("borrower")))
    has_bank     = sum(1 for a in auctions if _norm(a.get("bank")))
    has_area     = sum(1 for a in auctions if _norm(a.get("area")))
    has_city     = sum(1 for a in auctions if _norm(a.get("city")))
    has_total    = sum(1 for a in auctions if a.get("total_area"))
    has_parsed   = sum(1 for a in auctions if parse_total_area_sqft(a.get("total_area")))
    has_desc     = sum(1 for a in auctions if tokenize_description(a.get("description")))

    def _pct(x: int) -> str:
        return f"{x} ({100*x/n:.1f}%)" if n else f"{x}"

    print(f"  borrower set:          {_pct(has_borrower)}")
    print(f"  bank set:              {_pct(has_bank)}")
    print(f"  area set:              {_pct(has_area)}")
    print(f"  city set:              {_pct(has_city)}")
    print(f"  total_area populated:  {_pct(has_total)}")
    print(f"  total_area parseable:  {_pct(has_parsed)}")
    print(f"  description usable:    {_pct(has_desc)}")

    # Borrower duplicates (normalised).
    by_borrower: dict[str, list[dict]] = defaultdict(list)
    for a in auctions:
        b = _norm(a.get("borrower"))
        if b:
            by_borrower[b].append(a)
    repeats = [(b, aucts) for b, aucts in by_borrower.items() if len(aucts) > 1]
    repeats.sort(key=lambda x: -len(x[1]))
    print(f"\n=== Borrowers with >1 auction: {len(repeats)} ===")
    for b, aucts in repeats[:10]:
        print(f"  '{b}' — {len(aucts)} auctions")

    # Near-misses: same (borrower, bank, normalised-area) tuples — show the
    # within-group description Jaccard distribution so we can see whether
    # the threshold is well-calibrated.
    by_bba: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for a in auctions:
        br = _norm(a.get("borrower"))
        bk = _norm(a.get("bank"))
        ar = normalize_area(a.get("area"))
        if br and bk and ar:
            by_bba[(br, bk, ar)].append(a)
    bba_repeats = [(k, v) for k, v in by_bba.items() if len(v) > 1]
    bba_repeats.sort(key=lambda x: -len(x[1]))
    print(f"\n=== (borrower, bank, norm-area) candidates with >1 auction: {len(bba_repeats)} ===")
    sim_bins = [0, 0, 0, 0, 0]  # [0, .2, .4, .6, .8, 1]
    sim_total = 0
    for (br, bk, ar), aucts in bba_repeats[:5]:
        print(f"\n  borrower={br!r}  bank={bk!r}  area={ar!r}  ({len(aucts)} auctions)")
        toks = {a["auction_id"]: tokenize_description(a.get("description")) for a in aucts}
        for a in aucts[:6]:
            parsed = parse_total_area_sqft(a.get("total_area"))
            tok_count = len(toks[a["auction_id"]] or ())
            print(f"    {a['auction_id']}  total_area={a.get('total_area')!r} → "
                  f"{parsed if parsed else 'unparseable'}  desc_tokens={tok_count}")
        # Jaccard matrix for the first ~4 rows.
        sample = aucts[:4]
        for i in range(len(sample)):
            for j in range(i + 1, len(sample)):
                sa = toks[sample[i]["auction_id"]]
                sb = toks[sample[j]["auction_id"]]
                sim = jaccard(sa, sb)
                print(f"      sim({sample[i]['auction_id']}, {sample[j]['auction_id']}) = {sim:.2f}")

    # Full-population Jaccard histogram across ALL candidate within-group pairs.
    for _, aucts in bba_repeats:
        toks = {a["auction_id"]: tokenize_description(a.get("description")) for a in aucts}
        for i in range(len(aucts)):
            for j in range(i + 1, len(aucts)):
                sa = toks[aucts[i]["auction_id"]]
                sb = toks[aucts[j]["auction_id"]]
                if not sa or not sb:
                    continue
                sim = jaccard(sa, sb)
                sim_total += 1
                if sim < 0.2:   sim_bins[0] += 1
                elif sim < 0.4: sim_bins[1] += 1
                elif sim < 0.6: sim_bins[2] += 1
                elif sim < 0.8: sim_bins[3] += 1
                else:           sim_bins[4] += 1
    if sim_total:
        print(f"\n=== Description-Jaccard distribution across ALL "
              f"{sim_total} within-candidate pairs ===")
        labels = ["[0.0, 0.2)", "[0.2, 0.4)", "[0.4, 0.6)", "[0.6, 0.8)", "[0.8, 1.0]"]
        for label, count in zip(labels, sim_bins):
            pct = 100 * count / sim_total
            bar = "▓" * int(pct / 2)
            print(f"  {label}: {count:6d}  {pct:5.1f}%  {bar}")

    # Same (borrower, bank) ignoring area — catches area-name drift.
    by_bb: dict[tuple[str, str], set[str]] = defaultdict(set)
    areas_per_bb: dict[tuple[str, str], set[str]] = defaultdict(set)
    for a in auctions:
        br, bk, ar = _norm(a.get("borrower")), _norm(a.get("bank")), _norm(a.get("area"))
        if br and bk:
            by_bb[(br, bk)].add(a["auction_id"])
            if ar:
                areas_per_bb[(br, bk)].add(ar)
    area_drift = [(k, v, areas_per_bb[k])
                  for k, v in by_bb.items()
                  if len(v) > 1 and len(areas_per_bb[k]) > 1]
    area_drift.sort(key=lambda x: -len(x[1]))
    print(f"\n=== (borrower, bank) pairs with >1 auction AND differing areas: {len(area_drift)} ===")
    for (br, bk), ids, areas in area_drift[:5]:
        print(f"  borrower={br!r}  bank={bk!r}  → areas seen: {sorted(areas)}")


def write_pairs(session, pairs: list[tuple[str, str, str, str]]) -> int:
    written = 0
    for i in range(0, len(pairs), NEO4J_BATCH_SIZE):
        batch = pairs[i : i + NEO4J_BATCH_SIZE]
        rows = [
            {"a_id": a, "b_id": b, "reason": reason, "confidence": conf}
            for a, b, reason, conf in batch
        ]
        session.run(MERGE_PAIR, rows=rows)
        written += len(batch)
        print(f"  Linked pairs: [{written}/{len(pairs)}]", end="\r")
    return written


# ── CLI ──────────────────────────────────────────────────────────────────────

def run(
    dry_run: bool = False,
    rebuild: bool = False,
    debug: bool = False,
    sim_threshold: float = DEFAULT_SIM_THRESHOLD,
) -> None:
    if not (NEO4J_URI and NEO4J_USERNAME and NEO4J_PASSWORD):
        print("[ERROR] Neo4j credentials missing (NEO4J_URI / USERNAME / PASSWORD).")
        return

    t_start = time.time()
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    try:
        with driver.session(database=NEO4J_DATABASE) as session:
            print("Fetching candidate auctions from Neo4j...")
            auctions = fetch_auctions(session)
            print(f"  {len(auctions)} auctions loaded.")

            if debug:
                debug_diagnostics(auctions)
                return

            print(f"Matching re-auctions (sim_threshold={sim_threshold})...")
            pairs = find_reauction_pairs(auctions, sim_threshold=sim_threshold)
            clusters, expanded = expand_clusters(auctions, pairs)
            print(f"  {len(pairs)} direct pairs, "
                  f"{len(clusters)} transitive clusters, "
                  f"{len(expanded)} total pairs after expansion.")

            sizes = Counter(len(g) for g in clusters)
            if sizes:
                print("  Cluster-size histogram:")
                for size in sorted(sizes):
                    print(f"    {size} auctions: {sizes[size]} clusters")

            reasons = Counter(r[2] for r in expanded)
            if reasons:
                print("  Pair reasons:")
                for reason, n in reasons.most_common():
                    print(f"    {reason}: {n}")

            if dry_run:
                print("\n[DRY RUN] No writes performed.")
                return

            if rebuild:
                print("Dropping existing :SAME_PROPERTY_AS edges...")
                session.run(DROP_EXISTING)

            print("Writing :SAME_PROPERTY_AS edges...")
            written = write_pairs(session, expanded)
            print(f"\n  Wrote {written} pair operations "
                  f"({written * 2} directed edges merged).")

    finally:
        driver.close()

    elapsed = time.time() - t_start
    print(f"\n{'='*50}")
    print(f"  Time: {elapsed:.1f}s")
    print(f"{'='*50}")
    print("\nVerify:")
    print("  MATCH ()-[r:SAME_PROPERTY_AS]->() RETURN count(r)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Link re-auctioned properties.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Detect clusters and print stats; no writes.")
    parser.add_argument("--rebuild", action="store_true",
                        help="Delete existing :SAME_PROPERTY_AS edges first.")
    parser.add_argument("--debug", action="store_true",
                        help="Print data-coverage stats and near-miss groups "
                             "so we can see why matching isn't firing.")
    parser.add_argument("--sim-threshold", type=float,
                        default=DEFAULT_SIM_THRESHOLD,
                        help=f"Description Jaccard similarity cutoff for the "
                             f"borrower_location_desc rule (default: "
                             f"{DEFAULT_SIM_THRESHOLD}).")
    args = parser.parse_args()
    run(
        dry_run=args.dry_run,
        rebuild=args.rebuild,
        debug=args.debug,
        sim_threshold=args.sim_threshold,
    )


if __name__ == "__main__":
    main()
