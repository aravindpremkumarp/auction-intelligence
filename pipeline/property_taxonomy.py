"""Collapse free-text property types into one small, stable taxonomy.

Two very different inputs describe the same thing and must land in the same
buckets:

  1. LangExtract's `property` entity `property_type` attr — copied out of the
     sale notice, so it is unconstrained prose: 219 distinct values across the
     corpus ("land and building", "vacant house site", "manai with rcc building
     and land", "punjai land"). Classified here by ordered keyword rules.

  2. The auction portal's :PropertyType node name — a 23-value dropdown.
     Classified by a static table, because the value set is closed.

The portal taxonomy is NOT a fallback for a missing extraction. Its two most
common values, "Land" and "Plot", are what the listing form defaults to when
the seller picked nothing, and they agree with the notice text only 34-54% of
the time (against 97% for "Flat", which someone had to actively choose). A
property with no extracted type stays UNKNOWN; `portal_bucket` exists to show
provenance and to flag disagreement, not to fill gaps.

Rule order is the whole design. Form beats category (a flat in a commercial
complex is still a flat), a lot with any immovable anchor is immovable even
when machinery is listed alongside it, and the narrower category wins over the
broader one — so `industrial` is tested before `commercial`, and both before
the generic "has a building on it" rule that would otherwise swallow them.
"""
from __future__ import annotations

import re

# ── buckets ──────────────────────────────────────────────────────────────────

LAND = "land"                  # bare land, no structure mentioned
PLOT = "plot"                  # demarcated vacant plot / house site
HOUSE = "house"                # independent house, villa, or land + building
FLAT = "flat"                  # flat / apartment unit
AGRICULTURAL = "agricultural"
COMMERCIAL = "commercial"
INDUSTRIAL = "industrial"
MIXED = "mixed"                # explicit residential-cum-commercial etc.
MOVABLE = "movable"            # vehicles, machinery, stock — not real estate
UNKNOWN = "unknown"

BUCKETS = (LAND, PLOT, HOUSE, FLAT, AGRICULTURAL, COMMERCIAL, INDUSTRIAL,
           MIXED, MOVABLE, UNKNOWN)

# ── asset categories ─────────────────────────────────────────────────────────

RESIDENTIAL_CAT = "residential"
COMMERCIAL_CAT = "commercial"
INDUSTRIAL_CAT = "industrial"
AGRICULTURAL_CAT = "agricultural"
MIXED_CAT = "mixed"
MOVABLE_CAT = "movable"
UNKNOWN_CAT = "unknown"

# Bare land and plots are filed as residential: in this corpus they are almost
# always house sites in a residential layout, and anything genuinely farmland,
# industrial or commercial carries a word that routes it elsewhere first.
_CATEGORY_OF_BUCKET = {
    LAND: RESIDENTIAL_CAT,
    PLOT: RESIDENTIAL_CAT,
    HOUSE: RESIDENTIAL_CAT,
    FLAT: RESIDENTIAL_CAT,
    AGRICULTURAL: AGRICULTURAL_CAT,
    COMMERCIAL: COMMERCIAL_CAT,
    INDUSTRIAL: INDUSTRIAL_CAT,
    MIXED: MIXED_CAT,
    MOVABLE: MOVABLE_CAT,
    UNKNOWN: UNKNOWN_CAT,
}

# ── free-text classification ─────────────────────────────────────────────────

# "house site" / "house plot" name a *vacant* plot meant for building a house.
# The word "house" in them would otherwise trip the house rule, so they are
# rewritten to the bare form before any rule runs. Same for "non-agricultural",
# whose "agricul" substring would otherwise read as farmland.
_PRE_SUBS = (
    (re.compile(r"\bhouse\s*-?\s*sites?\b"), "site"),
    (re.compile(r"\bhouse\s*-?\s*plots?\b"), "plot"),
    (re.compile(r"\bhousing\s*sites?\b"), "site"),
    (re.compile(r"\bhousing\s*plots?\b"), "plot"),
    (re.compile(r"\bnon-?\s*agricultural\b"), "nonagri"),
)

# An immovable anchor means the lot includes real estate, so listed machinery
# is a fixture rather than the asset being sold.
_ANCHOR = re.compile(
    r"\bland\b|\blands\b|building|site|plot|flat|apartment|house|villa|shed|"
    r"godown|shop|structure|manai|terrace|premises|factory|mill|storage|"
    r"estate|property|construction"
)

_MOVABLE = re.compile(
    r"vehicle|\bcars?\b|machin|\bplant\b|equipment|fabric|\bstocks?\b|"
    r"trademark|securit|furniture|asset bundle|\bmovables?\b"
)

# Ordered: first match wins. See module docstring for why this order.
_RULES = (
    (FLAT, re.compile(r"\bflats?\b|apartments?")),
    (MIXED, re.compile(r"\bcum\b|mixed|residential and (commercial|industrial)|"
                       r"commercial and residential")),
    (INDUSTRIAL, re.compile(r"industrial|factory|\bmills?\b|\bsheds?\b")),
    (COMMERCIAL, re.compile(r"commercial|\bshops?\b|godown|hotel")),
    (AGRICULTURAL, re.compile(r"agricul|punjai|nanja")),
    (HOUSE, re.compile(r"\bhouses?\b|villa|buildings?|structures?|"
                       r"constructions?|improvements?|developments?|"
                       r"terraced?|tiled|manai")),
    (PLOT, re.compile(r"\bplots?\b|\bsites?\b")),
    (LAND, re.compile(r"\blands?\b")),
)

# Category words that survive when no form word is present ("residential
# property"): the bucket stays UNKNOWN but the category is still knowable.
_CATEGORY_HINTS = (
    (AGRICULTURAL_CAT, re.compile(r"agricul|punjai|nanja")),
    (INDUSTRIAL_CAT, re.compile(r"industrial|factory")),
    (COMMERCIAL_CAT, re.compile(r"commercial")),
    (RESIDENTIAL_CAT, re.compile(r"residential")),
)


def _clean(raw: str) -> str:
    text = re.sub(r"\s+", " ", (raw or "").strip().lower())
    for pattern, repl in _PRE_SUBS:
        text = pattern.sub(repl, text)
    return text


def classify_property_type(raw: str | None) -> str:
    """Map one free-text property type from a sale notice to a bucket.

    Returns UNKNOWN for empty input and for values that name no form at all
    ("immovable property", "mortgaged property").
    """
    text = _clean(raw or "")
    if not text:
        return UNKNOWN

    # Machinery named next to land is a fixture; machinery named alone is the
    # asset being sold.
    if _MOVABLE.search(text) and not _ANCHOR.search(text):
        return MOVABLE

    for bucket, pattern in _RULES:
        if pattern.search(text):
            return bucket
    return UNKNOWN


def asset_category(bucket: str, raw: str | None = None) -> str:
    """Category for a bucket. When the bucket is UNKNOWN, a category word left
    in the raw text ("residential property") still settles the category."""
    cat = _CATEGORY_OF_BUCKET.get(bucket, UNKNOWN_CAT)
    if cat != UNKNOWN_CAT:
        return cat
    text = _clean(raw or "")
    for category, pattern in _CATEGORY_HINTS:
        if pattern.search(text):
            return category
    return UNKNOWN_CAT


# ── portal dropdown classification ───────────────────────────────────────────

# Every :PropertyType node name the portal emits. Closed set, so exact-match on
# a casefolded key — a value outside this table is new and must be added here
# rather than guessed at, which is why the fallback is UNKNOWN.
_PORTAL_BUCKETS = {
    "land and building": HOUSE,
    "land": LAND,
    "plot": PLOT,
    "flat": FLAT,
    "house": HOUSE,
    "villa": HOUSE,
    "residential unit": HOUSE,
    "factory land and building": INDUSTRIAL,
    "industrial land & building": INDUSTRIAL,
    "industrial land": INDUSTRIAL,
    "cold storage land and building": INDUSTRIAL,
    "shed": INDUSTRIAL,
    "commercial property": COMMERCIAL,
    "commercial building": COMMERCIAL,
    "commercial shop": COMMERCIAL,
    "godown": COMMERCIAL,
    "agricultural land": AGRICULTURAL,
    "non- agricultural land": LAND,
    "non-agricultural land": LAND,
    "plant & machinery": MOVABLE,
    "vehicle": MOVABLE,
    "machinary": MOVABLE,
    "car": MOVABLE,
    "others": UNKNOWN,
}


def classify_portal_type(name: str | None) -> str:
    """Map a portal :PropertyType name to a bucket. Provenance and conflict
    detection only — never a substitute for the notice text."""
    if not name:
        return UNKNOWN
    return _PORTAL_BUCKETS.get(re.sub(r"\s+", " ", name.strip().lower()),
                               UNKNOWN)


def is_conflict(extracted: str, portal: str) -> bool:
    """True when both sides claim a real bucket and they disagree."""
    if extracted in (UNKNOWN, "") or portal in (UNKNOWN, ""):
        return False
    return extracted != portal
