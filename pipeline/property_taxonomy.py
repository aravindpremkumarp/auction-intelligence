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

#: A bucket the portal can only reach by describing bare ground. When the
#: notice says there is a BUILDING on it, the portal is not merely using a
#: different word — it is selling a house to someone searching for land.
_BARE_GROUND = frozenset({LAND, PLOT, AGRICULTURAL})
#: Buckets that describe a structure.
_BUILT = frozenset({HOUSE, FLAT, COMMERCIAL, INDUSTRIAL, MIXED})

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


# ── the schedule text ────────────────────────────────────────────────────────

# A lot's `full_description` is the notice's legal schedule, and the rules
# above must NEVER be run over it. That text says "land", "building", "plot"
# and "site" about the SAME property in consecutive clauses — an apartment's
# schedule describes the undivided share of land it sits on, the block built
# on it, and the unit itself. Keyword rules written for a six-word summary
# read that as four different property types.
#
# What the schedule does carry, unambiguously, is the unit's own
# identifier. "Flat No. S1" and "Villa No. 36B" are structured labels, not
# prose, and they name the form outright. Those are the only thing read here.
#: Only the flat marker is read. "Villa No." looks like its counterpart and
#: is not usable: the corpus spells "Village No." as "Villa No." often enough
#: that the one lot it would reclassify is a survey-number list in Agamcherry
#: Village, not a villa — and it earns no correct change anywhere. A marker
#: that buys nothing and costs a false positive does not go in.
_UNIT_MARKERS = (
    (FLAT, re.compile(r"\b(?:flats?|apartments?)\s*(?:bearing\s*)?"
                      r"(?:nos?\.?|numbers?|#)")),
)

# A boundary clause names the NEIGHBOURS: "Bounded on the North By: 18' Wide
# Passage, South By: Villa No. 7, East By: Villa No. 17". Read as the subject,
# a flat in a villa layout becomes a villa — one live lot did exactly that.
#
# Each clause is excised up to the next delimiter rather than truncating the
# text from the first boundary on: schedules routinely put the land parcel and
# ITS boundaries first and the unit itself in a later schedule, so truncating
# throws away the very sentence worth reading.
_BOUNDARY_CLAUSE = re.compile(
    r"\b(?:north|south|east|west)(?:ern)?\s*(?:side\s*)?(?:by)?\s*:?[^,;.]{0,80}")


def classify_from_schedule(text: str | None) -> str | None:
    """The form a schedule names via a unit identifier, or None.

    None when the text names no unit AND when it names two different ones:
    a schedule naming two forms describes more than one property, and picking
    either would be a guess — the same discipline the rival-lot gate applies
    to matching.
    """
    if not text:
        return None
    body = _BOUNDARY_CLAUSE.sub(" ", _clean(text))
    hits = {bucket for bucket, pattern in _UNIT_MARKERS
            if pattern.search(body)}
    return hits.pop() if len(hits) == 1 else None


#: Buckets a schedule is allowed to correct. Bare ground and UNKNOWN are the
#: states that lose a buyer outright — a flat filed as "vacant house site" is
#: invisible to every flat search. HOUSE is included because "land and
#: building" is what the extractor writes when it summarises a flat's
#: schedule (undivided share of land, plus the unit) without naming the unit.
#:
#: Everything else is left alone. FLAT, COMMERCIAL, INDUSTRIAL and MIXED are
#: specific claims someone made deliberately, and overriding them destroys
#: information rather than recovering it: one live lot reading "Residential
#: Flat & Commercial Shop" is genuinely mixed, and one reading "service
#: Apartments ... at commercial complex & Hotel" is genuinely commercial.
_SCHEDULE_MAY_CORRECT = _BARE_GROUND | {HOUSE, UNKNOWN}


def classify_lot_type(raw: str | None, description: str | None = None) -> str:
    """The bucket for one lot: its stated type, corrected by the schedule.

    The extractor's `property_type` is a six-word paraphrase; the schedule is
    the notice itself. When the paraphrase says bare ground (or nothing) and
    the schedule names a unit outright, the schedule wins — nine live lots
    whose schedule reads "Flat No. S1, 1149 sq.ft" are filed as "vacant house
    site" or "land", unfindable by anyone searching for a flat.

    Correction only ever runs in that direction, and only when a schedule
    names a unit, so this is a no-op for 99.7% of lots.
    """
    stated = classify_property_type(raw)
    if stated not in _SCHEDULE_MAY_CORRECT:
        return stated
    from_schedule = classify_from_schedule(description)
    return from_schedule or stated


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


#: Buckets that name the same thing for conflict purposes. `land` and `plot`
#: both mean "bare ground, no structure" — the split between them is how the
#: ground is described (a demarcated house site vs an undivided parcel), not
#: what is being sold, and the two sources routinely pick different words for
#: one property. Counting that as a disagreement put 226 rows in front of a
#: reviewer with nothing to decide, and buried the 666 that matter.
_SAME_THING = ({LAND, PLOT},)


def buckets_agree(a: str, b: str) -> bool:
    """True when two buckets name the same kind of property."""
    if a == b:
        return True
    return any(a in group and b in group for group in _SAME_THING)


def is_conflict(extracted: str, portal: str) -> bool:
    """True when both sides claim a real bucket and they mean different things.

    Synonymous buckets do NOT conflict — see `_SAME_THING`.
    """
    if extracted in (UNKNOWN, "") or portal in (UNKNOWN, ""):
        return False
    return not buckets_agree(extracted, portal)


def effective_bucket(extracted: str | None, portal: str | None) -> str:
    """The bucket a SEARCH should treat this listing as.

    The notice wins — it is the legal document, and the portal's Land/Plot
    default agrees with it only 34-54% of the time. But a listing no
    extraction reached would become unfindable by type if the portal value
    were simply discarded (99 listings live), so the portal is the fallback
    and never the override.

    Deliberately NOT the same as `property_type_norm`, which stays purely
    notice-derived so provenance is never muddied: a value that fell back is
    a search convenience, not a claim about what the notice said.
    """
    if extracted and extracted != UNKNOWN:
        return extracted
    return portal or UNKNOWN


def resolve_bucket(value: str | None) -> str:
    """Resolve one caller-supplied property-type name to a bucket.

    Search callers speak three vocabularies and all three must work, so they
    are tried in order of how much each name is trusted:

      1. an exact bucket name — what the facet and the agent enum hand out;
      2. the portal's closed dropdown table — what links and bookmarks made
         before the search moved off the portal edge still carry;
      3. the notice classifier's keyword rules — anything hand-typed, which
         is what turns "Apartment" or "house site" into a real bucket instead
         of into no results.

    UNKNOWN when none of them recognises it. Callers must turn that into
    "matches nothing", never into "matches everything": silently dropping the
    filter reports a wider result set as if it had been filtered.
    """
    if not value:
        return UNKNOWN
    if value in BUCKETS:
        return value
    portal = classify_portal_type(value)
    if portal != UNKNOWN:
        return portal
    return classify_property_type(value)


def search_buckets(bucket: str) -> list[str]:
    """Every bucket a request for `bucket` should also match.

    Someone filtering for land means bare ground, and whether a notice called
    it a plot or a parcel is not a distinction they asked for — the same
    equivalence `is_conflict` uses, applied to the query side.
    """
    for group in _SAME_THING:
        if bucket in group:
            return sorted(group)
    return [bucket]


def conflict_severity(extracted: str, portal: str) -> str | None:
    """How much a disagreement costs a buyer. None when there is none.

    'critical' — the portal says bare ground and the notice describes a
    building (or the reverse). This is the one that misleads a search: 666
    live listings today, 139 of them flats filed under Land or Plot.
    'med'      — both agree something is built, or both that nothing is, but
    they differ on what kind. Worth fixing, not misleading.
    """
    if not is_conflict(extracted, portal):
        return None
    crossed = ((extracted in _BUILT and portal in _BARE_GROUND)
               or (extracted in _BARE_GROUND and portal in _BUILT))
    return "critical" if crossed else "med"
