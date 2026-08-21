"""
api/agent3/enums.py
-------------------
The graph's live vocabulary, snapshotted 21 Aug 2026, with the counts that
justify each one being a filter at all.

These are rendered into tool docstrings rather than into the system prompt.
The v2 lesson: the first narrowing run put "Residential" on `property_type`
(a valid value, wrong field — it belongs on `asset_category`) and ran a whole
conversation on zero rows. Schema next to the parameter fixed it, and a
docstring rides in the cached tool schema instead of the per-turn prompt.

Anything whose coverage is too thin to filter on is recorded here as a
comment rather than exposed, so nobody re-adds it hopefully.
"""
from __future__ import annotations

#: 3 values, on 2,964 of 2,964 listings. What people mean by
#: "residential/commercial/industrial" — NOT a property_type.
ASSET_CATEGORIES = ("Residential", "Commercial", "Industrials")

#: 23 live values, verbatim casing and spacing (note the space in
#: "Non- Agricultural Land" — it is in the data, not a typo here).
PROPERTY_TYPES = (
    "Agricultural Land", "Car", "Cold Storage Land And Building",
    "Commercial Building", "Commercial Property", "Commercial Shop",
    "Factory land and Building", "Flat", "Godown", "House",
    "Industrial Land", "Industrial Land & Building", "Land",
    "Land And Building", "Machinary", "Non- Agricultural Land", "Others",
    "Plant & Machinery", "Plot", "Residential Unit", "Shed", "Vehicle",
    "Villa",
)

#: 4 values. SARFAESI is ~98% of typed rows and the default.
AUCTION_TYPES = (
    "SARFAESI Auction", "DRT Auction", "Liquidation Auction",
    "Private Property",
)

#: On the Document. 1,609 of 1,628 notices carry one.
LEGAL_FRAMEWORKS = ("SARFAESI", "DRT", "IBC", "other")

#: 1,985 lots (60%). The rel carries `taken_on`.
#: physical    — bank holds the keys; buyer can usually take possession.
#: symbolic    — paper possession only; buyer may face an eviction process.
#: constructive— rare middle ground; read the notice.
POSSESSION_TYPES = ("physical", "symbolic", "constructive")

#: Boundary.access_kind, 10,329 boundaries on 2,595 lots.
ACCESS_KINDS = ("road", "street", "pathway", "plot", "channel", "setback")

#: Measurement kinds, by volume. `is_headline` marks the one extent that
#: describes the lot itself — prefer it over picking a kind by hand.
#: uds/uds_parent are a flat's undivided share of the parent plot and are
#: NOT floor area; conflating them makes a 760 sqft flat look like 2,257.
EXTENT_KINDS = (
    "extent", "total", "built_up", "uds", "uds_parent", "super_built_up",
    "carpet",
)

#: Units seen on Measurement. sqft_norm normalises all of them.
UNITS = ("sq_ft", "sq_m", "cent", "ground", "acre", "are", "hectare")

#: Identifier.kind, 10,253 identifiers on 3,215 lots (96%).
IDENTIFIER_KINDS = (
    "survey_old", "survey_new", "patta", "plot", "door_old", "door_new",
    "sale_deed", "approved_layout", "property_id", "flat", "assessment_old",
    "assessment_new", "block", "cersai", "floor", "ward_no", "chitta",
    "khata",
)

#: How users say it -> what the graph calls it. Expanded before the query,
#: so a miss is a widened search rather than zero rows.
PROPERTY_TYPE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "independent house": ("House", "Villa", "Land And Building"),
    "house": ("House", "Villa", "Land And Building"),
    "bungalow": ("Villa", "House"),
    "apartment": ("Flat", "Residential Unit"),
    "flat": ("Flat", "Residential Unit"),
    "plot": ("Plot", "Land", "Non- Agricultural Land"),
    "land": ("Land", "Plot", "Non- Agricultural Land", "Agricultural Land"),
    "farm": ("Agricultural Land",),
    "farmland": ("Agricultural Land",),
    "shop": ("Commercial Shop", "Commercial Property"),
    "office": ("Commercial Building", "Commercial Property"),
    "warehouse": ("Godown", "Shed"),
    "godown": ("Godown", "Shed"),
    "factory": ("Factory land and Building", "Industrial Land & Building"),
    "industrial": ("Industrial Land", "Industrial Land & Building",
                   "Factory land and Building"),
}

#: Sort keys accepted by find_properties.
SORT_KEYS = (
    "deadline", "auction_date", "price_asc", "price_desc", "area_desc",
    "recent",
)

#: group_by dimensions. Each maps to (cypher value expression, optional match).
GROUP_BY_DIMENSIONS = (
    "city", "area", "district", "taluk", "bank", "property_type",
    "asset_category", "auction_type", "platform", "price_band", "month",
    "attempt_no", "possession",
)

# Deliberately NOT exposed as filters, recorded so they are not re-added:
#   Lot.occupancy_status      — 33 lots. A footnote, not a filter.
#   Lot.latitude/longitude    — 171 lots. No radius search is possible.
#   Auction.outcome           — only ever "unsold". There are NO sold prices
#                               in this graph; "did it sell / what did it
#                               fetch" is a refusal, not a feature.
#   Lot.construction_type     — 188 lots.
#   Lot.landmark              — 86 lots.


def expand_property_types(values: str | list[str] | None) -> list[str] | None:
    """Map user phrasing onto live PropertyType names.

    An exact enum value passes through untouched. An unknown word that has no
    synonym also passes through — the caller gets zero rows plus a `relax`
    hint naming the filter, which is more useful than a silent widening.
    """
    if values is None:
        return None
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for v in values:
        raw = str(v).strip()
        if raw in PROPERTY_TYPES:
            out.append(raw)
            continue
        mapped = PROPERTY_TYPE_SYNONYMS.get(raw.lower())
        if mapped:
            out.extend(mapped)
        else:
            out.append(raw)
    # Stable de-dupe: the order the model asked for is the order it reads back.
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]
