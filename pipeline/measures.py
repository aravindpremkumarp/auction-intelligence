"""Physical quantities from auction-notice text: areas, lengths, road widths.

Pure functions — no I/O, no Neo4j — so they unit-test cheaply and can be reused
by the promotion loader, the review UI and any backfill.

Three jobs, all of which the extraction model was previously asked to do (and
did badly for anything that wasn't already in square feet):

  parse_area()      "6.00 Cents"           -> (6.0, 'cent', 2613.6 sq.ft)
  parse_length()    "34'9 ft"              -> 34.75 ft
  read_adjacency()  "23 Feet wide E-W Road"-> ('road', 23.0)

Why this is code and not a prompt: across 734 extracted extents, 41% carried no
normalized sq.ft figure, and the misses were almost entirely non-sq-ft units
(acre 79% missing, are 78%, cent 54%, hectare 100% — square feet only 2%).
Conversion is deterministic arithmetic; asking an LLM to do it buys nothing and
loses a third of the corpus.

UNIT MATCHING IS LONGEST-FIRST, and that is load-bearing:
  "square feet" contains the substring "are"  -> naive matching converts sq.ft
                                                 as ares, a 1076x error
  "hectare"     contains the substring "are"  -> same trap, 100x
("acre" does NOT contain "are" — the collisions are `squARE` and `hectARE`.)
Aliases are sorted by descending length and matched on word boundaries, so the
longest real unit always wins.
"""
from __future__ import annotations

import re

# ── units ────────────────────────────────────────────────────────────────────
# to_sqft is the multiplier onto square feet. Values are exact where the
# definition is exact (1 acre = 43,560 sq.ft) and to 6 s.f. otherwise.
#
# `ground` is a Chennai-specific unit (2,400 sq.ft) that still appears in TN
# notices. `cent` (1/100 acre) and `are` (100 sq.m) are the two that dominate
# the non-sq-ft tail in this corpus.
UNITS: dict[str, float] = {
    "sq_ft":   1.0,
    "sq_m":    10.763910,
    "sq_yard": 9.0,
    "cent":    435.6,
    "acre":    43560.0,
    "are":     1076.391,
    "hectare": 107639.104,
    "ground":  2400.0,
}

# Surface forms seen in notices, mapped to the canonical unit name. Order in
# this dict is irrelevant — _ALIAS_RE sorts by length descending.
UNIT_ALIASES: dict[str, str] = {
    # square feet
    "square feet": "sq_ft", "square foot": "sq_ft", "sq feet": "sq_ft",
    "sq.ft": "sq_ft", "sq ft": "sq_ft", "sqft": "sq_ft", "sq.feet": "sq_ft",
    "s.ft": "sq_ft", "sft": "sq_ft", "sq.f": "sq_ft",
    # square metres
    "square metres": "sq_m", "square meters": "sq_m", "square metre": "sq_m",
    "square meter": "sq_m", "sq.mtrs": "sq_m", "sq.mtr": "sq_m",
    "sq.mts": "sq_m", "sq.mt": "sq_m", "sq mtr": "sq_m", "sq.m": "sq_m",
    "sq m": "sq_m", "sqm": "sq_m",
    # square yards
    "square yards": "sq_yard", "square yard": "sq_yard",
    "sq.yds": "sq_yard", "sq.yd": "sq_yard", "sq yard": "sq_yard",
    # land units
    "hectares": "hectare", "hectare": "hectare", "hect": "hectare",
    "acres": "acre", "acre": "acre",
    "cents": "cent", "cent": "cent",
    "ares": "are", "are": "are",
    "grounds": "ground", "ground": "ground",
}

# Longest alias first so "square feet" is consumed before the "are" inside it.
_ALIAS_RE = re.compile(
    r"(?<![a-z])(" + "|".join(
        re.escape(a) for a in sorted(UNIT_ALIASES, key=len, reverse=True)
    ) + r")(?![a-z])",
    re.IGNORECASE,
)

# Unicode vulgar fractions the OCR prompt is told to preserve verbatim.
_FRACTIONS = {
    "½": 0.5, "¼": 0.25, "¾": 0.75,
    "⅓": 1 / 3, "⅔": 2 / 3, "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
}

_FT_PER_M = 3.280840


def _to_float(tok: str) -> float | None:
    try:
        return float(tok.replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_quantity(raw: str) -> float | None:
    """First numeric quantity in `raw`, honouring fractions.

    Handles "6.00", "2,180", "16 1/2", "2 ½" and a bare "½". Returns None when
    there is no number at all. Deliberately takes the FIRST number: notices
    phrase a part-of-whole as "Acre 6.00 cents out of Acre 7.86 Cents", where
    the leading figure is the parcel being sold.
    """
    if not raw:
        return None
    s = str(raw)

    # leading integer/decimal followed by an ASCII or unicode fraction
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(\d+)\s*/\s*(\d+)", s)
    if m:
        whole, num, den = _to_float(m.group(1)), _to_float(m.group(2)), _to_float(m.group(3))
        if whole is not None and num is not None and den:
            return whole + num / den

    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*([" + "".join(_FRACTIONS) + r"])", s)
    if m:
        whole = _to_float(m.group(1))
        if whole is not None:
            return whole + _FRACTIONS[m.group(2)]

    m = re.search(r"(\d+)\s*/\s*(\d+)", s)
    if m:
        num, den = _to_float(m.group(1)), _to_float(m.group(2))
        if num is not None and den:
            return num / den

    m = re.search(r"\d[\d,]*(?:\.\d+)?", s)
    if m:
        return _to_float(m.group(0))

    for ch, val in _FRACTIONS.items():
        if ch in s:
            return val
    return None


def _normalize_for_units(s: str) -> str:
    """Collapse the spacing variants notices use inside a unit token.

    OCR yields "Sq. ft", "Sq . Ft" and "sq  ft" for the same unit; folding the
    space after a period (and runs of whitespace) means the alias table lists
    real units rather than every way a scanner can space them.
    """
    s = re.sub(r"\s*\.\s*", ".", s)
    return re.sub(r"\s{2,}", " ", s)


def detect_unit(raw: str) -> str | None:
    """Canonical unit name for the first unit token in `raw`, else None.

    Longest-alias-first matching is what stops "square feet" being read as
    "are" (1076x) and "hectare" as "are" (100x).
    """
    if not raw:
        return None
    m = _ALIAS_RE.search(_normalize_for_units(str(raw)))
    return UNIT_ALIASES[m.group(1).lower()] if m else None


def to_sqft(value: float | None, unit: str | None) -> float | None:
    """Convert `value` in `unit` to square feet. None if either is unknown."""
    if value is None or unit is None:
        return None
    factor = UNITS.get(unit)
    return round(value * factor, 2) if factor is not None else None


def parse_area(raw: str) -> tuple[float | None, str | None, float | None]:
    """(value, canonical_unit, sqft) for an area string.

    >>> parse_area("6.00 Cents")
    (6.0, 'cent', 2613.6)
    >>> parse_area("2180 Sq. ft")
    (2180.0, 'sq_ft', 2180.0)
    """
    value = parse_quantity(raw)
    unit = detect_unit(raw)
    return value, unit, to_sqft(value, unit)


# ── lengths (boundary dimensions) ────────────────────────────────────────────

# A boundary measurement is a LENGTH. Seven extracted values were areas
# ("19 Sq.Ft") — the model grabbed the wrong span. Catch them at load time
# rather than letting them corrupt plot-shape maths.
_AREA_HINT_RE = re.compile(r"\bsq\b|\bsq[\. ]?(ft|m|mt|yd|feet)|square", re.IGNORECASE)

# 34'9 / 34' 9" — feet and inches, common in TN survey text.
_FT_IN_RE = re.compile(r"(\d+)\s*['′]\s*(\d+)\s*[\"″]?")


def is_length(raw: str) -> bool:
    """False when an AREA was written into a length field."""
    return bool(raw) and not _AREA_HINT_RE.search(str(raw))


def parse_length(raw: str) -> float | None:
    """Boundary dimension in feet, or None if absent/invalid.

    Returns None for area-shaped values so `is_length_valid` can flag them
    instead of silently producing a wrong number.
    """
    if not raw or not is_length(raw):
        return None
    s = str(raw)

    m = _FT_IN_RE.search(s)
    if m:
        feet, inches = _to_float(m.group(1)), _to_float(m.group(2))
        if feet is not None and inches is not None:
            return round(feet + inches / 12.0, 3)

    value = parse_quantity(s)
    if value is None:
        return None

    # metric only when a metre token is present and no feet token is
    if re.search(r"\b(m|mtr|mtrs|meter|metre|meters|metres)\b", s, re.IGNORECASE) \
            and not re.search(r"\b(ft|feet|foot)\b", s, re.IGNORECASE):
        return round(value * _FT_PER_M, 3)
    return round(value, 3)


# ── boundary adjacency: access kind + road width ─────────────────────────────

# Checked in order — the setback patterns MUST precede the road patterns.
# "30 FT LAND LEFT BY ROAD" contains the word "road" but means land reserved
# for future road-widening, which REDUCES the usable parcel. Reading it as
# frontage inverts its meaning.
_ACCESS_PATTERNS: tuple[tuple[str, str], ...] = (
    ("setback",  r"land\s+left\s+(by|for)\s+road|road\s+wid[ei]|set\s*-?\s*back|"
                 r"reserved\s+for\s+road"),
    ("pathway",  r"path\s*way|pathway|\bpath\b|cart\s*track|passage"),
    ("street",   r"\bstreet\b|\blane\b|\bbazaar\b"),
    ("road",     r"\broads?\b|\bhighway\b|\bnh\b|\bsh\b|main\s+road"),
    ("channel",  r"\bchannel\b|\bcanal\b|\bodai\b|\bnala\b|\bdrain\b"),
    ("plot",     r"\bplot\b|\bsurvey\b|\bs\.?\s*no\b|land\s+of|property\s+of|"
                 r"house\s+of|belonging\s+to"),
)

# A width stated before the access noun: "23 Feet wide East-West Road".
_WIDTH_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(?:['′]\s*)?"
    r"(ft|feet|foot|mtr|mtrs|m|meter|metre|meters|metres)\b",
    re.IGNORECASE,
)


def read_adjacency(raw: str) -> tuple[str | None, float | None]:
    """(access_kind, road_width_ft) for what abuts one side of a parcel.

    Road width is a valuation input — it governs vehicle access and, in many
    municipal rules, permissible setback and FSI. In the extracted corpus it
    sits inside 178 adjacency strings where nothing can query it.

    >>> read_adjacency("23 Feet wide East-West Road")
    ('road', 23.0)
    >>> read_adjacency("30 FT LAND LEFT BY ROAD")
    ('setback', 30.0)
    """
    if not raw:
        return None, None
    s = str(raw)

    kind = None
    for name, pattern in _ACCESS_PATTERNS:
        if re.search(pattern, s, re.IGNORECASE):
            kind = name
            break

    width = None
    m = _WIDTH_RE.search(s)
    if m:
        value = _to_float(m.group(1))
        if value is not None:
            metric = m.group(2).lower() in {"m", "mtr", "mtrs", "meter",
                                            "metre", "meters", "metres"}
            width = round(value * _FT_PER_M, 2) if metric else round(value, 2)

    # A width only means "road width" for things you can travel along; the
    # number in "Plot of Mr.X 40 ft" is that neighbour's dimension, not access.
    if kind not in {"road", "street", "pathway", "setback", "channel"}:
        width = None
    return kind, width


# ── headline extent selection ────────────────────────────────────────────────

# Which measurement price-per-sqft may divide by, best first, per property
# type. uds_parent is absent from every list on purpose: it is the whole
# apartment plot a flat holds a share of, so dividing one flat's price by it
# understates price/sqft by an order of magnitude.
_HEADLINE_ORDER: dict[str, tuple[str, ...]] = {
    "flat":      ("built_up", "super_built_up", "carpet"),
    "apartment": ("built_up", "super_built_up", "carpet"),
    "_default":  ("total", "extent", "built_up"),
}

# Land and plots have no structure, so their own extent is the only honest
# denominator. House/villa legitimately have BOTH a land extent and a built-up
# area; we take land, because that is what comparables are quoted on — but it
# is a judgement call, recorded here rather than buried in a query.
_LAND_LIKE = {"land", "plot", "agricultural", "agriculture", "vacant",
              "house", "villa", "bungalow", "industrial", "godown", "commercial"}


def pick_headline(kinds: dict[str, float | None],
                  property_type: str | None) -> str | None:
    """Name of the measurement kind to use as the price-per-sqft denominator.

    `kinds` maps measurement kind -> sqft_norm (None when unconvertible); only
    kinds with a usable number are eligible.
    """
    usable = {k for k, v in kinds.items() if v}
    if not usable:
        return None
    pt = (property_type or "").strip().lower()
    if pt in _HEADLINE_ORDER:
        order = _HEADLINE_ORDER[pt]
    elif any(w in pt for w in _LAND_LIKE):
        order = _HEADLINE_ORDER["_default"]
    elif "uds" in kinds or "uds_parent" in kinds:
        # Only a flat holds an undivided share of the land beneath it, so the
        # presence of a UDS identifies one even when the notice never states a
        # property_type — which is the common case: 109 lots carry a UDS while
        # property_type is frequently absent. Without this the default order
        # would pick `total`/`extent`, i.e. the land rather than the dwelling.
        order = _HEADLINE_ORDER["flat"]
    else:
        order = _HEADLINE_ORDER["_default"]
    for kind in order:
        if kind in usable:
            return kind
    return None
