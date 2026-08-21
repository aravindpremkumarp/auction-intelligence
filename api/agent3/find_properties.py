"""
api/agent3/find_properties.py
-----------------------------
The search tool. One call answers any find / count / break-down question.

Two things make it different from `search_auctions`:

**It can see the sale notice.** Filters reach through
`(a)-[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->(:Lot)` into extent, possession,
road width, access kind, encumbrance, secured outstanding, re-auction attempt
and survey/patta/door identifiers. "Residential plots in Coimbatore over 2,000
sqft where the bank has physical possession" is one call here and impossible
today.

**It answers the follow-up in the same round trip.** `refine` (live, non-empty
narrowings with counts) and `relax` (on zero rows, which single filter to drop
and what it unlocks) are computed alongside the result. The old loop found 400
matches and then fired more searches to work out how to narrow — that is the
token cost this removes, and it costs one extra database query, not one extra
model call.

Every lot-derived value is scope-tagged. See `api/agent3/common.py::scope_of`.
"""
from __future__ import annotations

from datetime import datetime

from api.agent3 import enums
from api.agent3.common import (
    SQFT_CEIL, SQFT_FLOOR, ToolInputError, ToolSink, aware, clamp_limit,
    json_safe, now_utc, require_enum, scope_of, tool,
)
# Identifier resolution is shared with the standalone `find_by_identifier`
# tool — both the Lucene escaping and the dual-path (Lot / Parcel) query live
# in api/agent3/identifiers.py so they cannot drift into two answers for the
# same survey number. Imported as a module-level name so tests can still
# `monkeypatch.setattr(FP, "resolve_identifier", ...)`.
from api.agent3.identifiers import resolve_identifier
from api.neo4j_client import run_read_query

#: Rows the panel may hold. The model never sees these — see ToolSink.
PANEL_ROW_CAP = 500

#: The lot subgraph every lot-layer filter hangs off.
_LOT_PATH = "(a)-[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->(l:Lot)"

_PRICE_BAND_CASE = """CASE
      WHEN a.reserve_price_num IS NULL THEN 'unknown'
      WHEN a.reserve_price_num < 1000000 THEN 'under 10L'
      WHEN a.reserve_price_num < 3000000 THEN '10L-30L'
      WHEN a.reserve_price_num < 6000000 THEN '30L-60L'
      WHEN a.reserve_price_num < 10000000 THEN '60L-1Cr'
      ELSE 'above 1Cr' END"""

#: group_by dimension -> (optional MATCH pattern, value expression).
_GROUP_BY: dict[str, tuple[str | None, str]] = {
    "city": ("(a)-[:LOCATED_IN_CITY]->(g:City)", "g.name"),
    "area": ("(a)-[:LOCATED_IN_AREA]->(g:Area)", "g.name"),
    "district": ("(a)-[:LOCATED_IN_DISTRICT]->(g:District)", "g.name"),
    "taluk": ("(a)-[:LOCATED_IN_TALUK]->(g:Taluk)", "g.name"),
    "bank": ("(a)-[:CONDUCTED_BY]->(g:Bank)", "g.name"),
    "property_type": ("(a)-[:HAS_PROPERTY_TYPE]->(g:PropertyType)", "g.name"),
    "asset_category": ("(a)-[:HAS_ASSET_CATEGORY]->(g:AssetCategory)", "g.name"),
    "auction_type": ("(a)-[:IS_AUCTION_TYPE]->(g:AuctionType)", "g.name"),
    "platform": ("(a)-[:HAS_DOCUMENT]->(:Document)-[:HOSTED_ON]->(g:Platform)", "g.name"),
    "possession": (f"{_LOT_PATH}-[:POSSESSION_IS]->(g:PossessionType)", "g.name"),
    "price_band": (None, _PRICE_BAND_CASE),
    "month": (None, "substring(toString(a.auction_start_dt), 0, 7)"),
    "attempt_no": (f"{_LOT_PATH}-[:OFFERED_IN]->(g:Auction)", "toString(g.attempt_no)"),
}

_SORTS: dict[str, str] = {
    "deadline": "a.application_deadline_dt IS NULL, a.application_deadline_dt ASC",
    "auction_date": "a.auction_start_dt IS NULL, a.auction_start_dt ASC",
    "price_asc": "a.reserve_price_num IS NULL, a.reserve_price_num ASC",
    "price_desc": "a.reserve_price_num DESC",
    "area_desc": "sqft_max DESC",
    "recent": "a.auction_start_dt DESC",
}


class _Query:
    """Accumulates MATCH/WHERE fragments so every query shares one filter set.

    The count, the rows, the refine branches and the relax branches must all
    see identical filters or the numbers disagree with each other — which is
    worse than being slow, because it is invisible.
    """

    def __init__(self) -> None:
        self.joins: list[str] = []
        self.where: list[str] = []
        self.params: dict = {}
        #: Filters that were applied at the notice level, not the lot level.
        self.notice_level: list[str] = []
        #: Human-readable list of what is currently constraining the search,
        #: used to build `relax` and to echo the scope back in the answer.
        self.active: list[tuple[str, str]] = []

    def add(self, label: str, description: str, *, join: str | None = None,
            where: str | None = None, **params) -> None:
        if join:
            self.joins.append(join)
        if where:
            self.where.append(where)
        self.params.update(params)
        self.active.append((label, description))

    def base(self) -> str:
        parts = ["MATCH (a:AuctionProperty)"]
        parts.extend(f"MATCH {j}" for j in self.joins)
        if self.where:
            parts.append("WHERE " + "\n  AND ".join(self.where))
        return "\n".join(parts)

    def base_without(self, label: str) -> tuple[str, dict]:
        """The same query with one filter removed — the `relax` mechanism."""
        other = _Query()
        other.params = dict(self.params)
        for lbl, join, where in self._fragments:
            if lbl == label:
                continue
            if join:
                other.joins.append(join)
            if where:
                other.where.append(where)
        return other.base(), other.params

    # Fragments are recorded per filter so one can be dropped cleanly.
    _fragments: list[tuple[str, str | None, str | None]]


def _build(  # noqa: PLR0912, PLR0913, PLR0915 - one filter, one branch
    *, city, area, district, taluk, revenue_village, asset_category,
    property_type, auction_type, reserve_price_min, reserve_price_max,
    emd_min, emd_max, auction_from, auction_to, deadline_before, upcoming_only,
    bank, branch, borrower, platform, legal_framework, area_sqft_min,
    area_sqft_max, possession, road_width_ft_min, access_kind,
    has_encumbrance_note, outstanding_max, attempt_no, reauction_only,
    identifier, identifier_kind,
) -> _Query:
    q = _Query()
    q._fragments = []

    def add(label, desc, *, join=None, where=None, notice_level=False, **params):
        q._fragments.append((label, join, where))
        q.add(label, desc, join=join, where=where, **params)
        if notice_level:
            q.notice_level.append(label)

    # ── place ────────────────────────────────────────────────────────────
    if city:
        add("city", f"city = {city}",
            join="(a)-[:LOCATED_IN_CITY]->(_city:City)",
            where="toLower(_city.name) = toLower($city)", city=str(city).strip())
    if area:
        add("area", f"area contains {area}",
            join="(a)-[:LOCATED_IN_AREA]->(_area:Area)",
            where="toLower(_area.name) CONTAINS toLower($area)",
            area=str(area).strip())
    if district:
        add("district", f"district = {district}",
            join="(a)-[:LOCATED_IN_DISTRICT]->(_dist:District)",
            where="toLower(_dist.name) CONTAINS toLower($district)",
            district=str(district).strip())
    if taluk:
        add("taluk", f"taluk = {taluk}",
            join="(a)-[:LOCATED_IN_TALUK]->(_tal:Taluk)",
            where="toLower(_tal.name) CONTAINS toLower($taluk)",
            taluk=str(taluk).strip())
    if revenue_village:
        add("revenue_village", f"revenue village = {revenue_village}",
            join="(a)-[:LOCATED_IN_REVENUE_VILLAGE]->(_rv:RevenueVillage)",
            where="toLower(_rv.name) CONTAINS toLower($revenue_village)",
            revenue_village=str(revenue_village).strip())

    # ── what ─────────────────────────────────────────────────────────────
    if asset_category:
        ac = require_enum(asset_category, enums.ASSET_CATEGORIES, "asset_category")
        add("asset_category", f"asset category = {ac}",
            join="(a)-[:HAS_ASSET_CATEGORY]->(_ac:AssetCategory)",
            where="_ac.name = $asset_category", asset_category=ac)
    if property_type:
        pts = enums.expand_property_types(property_type)
        add("property_type", f"property type in {pts}",
            join="(a)-[:HAS_PROPERTY_TYPE]->(_pt:PropertyType)",
            where="_pt.name IN $property_types", property_types=pts)
    if auction_type:
        at = require_enum(auction_type, enums.AUCTION_TYPES, "auction_type")
        add("auction_type", f"auction type = {at}",
            join="(a)-[:IS_AUCTION_TYPE]->(_at:AuctionType)",
            where="_at.name = $auction_type", auction_type=at)

    # ── money ────────────────────────────────────────────────────────────
    if reserve_price_min is not None:
        add("reserve_price_min", f"reserve >= {reserve_price_min:,.0f}",
            where="a.reserve_price_num >= $rp_min",
            rp_min=float(reserve_price_min))
    if reserve_price_max is not None:
        add("reserve_price_max", f"reserve <= {reserve_price_max:,.0f}",
            where="a.reserve_price_num <= $rp_max",
            rp_max=float(reserve_price_max))
    if emd_min is not None:
        add("emd_min", f"EMD >= {emd_min:,.0f}",
            where="a.emd_num >= $emd_min", emd_min=float(emd_min))
    if emd_max is not None:
        add("emd_max", f"EMD <= {emd_max:,.0f}",
            where="a.emd_num <= $emd_max", emd_max=float(emd_max))

    # ── time ─────────────────────────────────────────────────────────────
    if upcoming_only:
        add("upcoming_only", "auction date is in the future",
            where="a.auction_start_dt >= $now", now=now_utc())
    if auction_from is not None:
        add("auction_from", f"auction on/after {auction_from}",
            where="a.auction_start_dt >= $auction_from",
            auction_from=aware(auction_from))
    if auction_to is not None:
        add("auction_to", f"auction on/before {auction_to}",
            where="a.auction_start_dt <= $auction_to",
            auction_to=aware(auction_to))
    if deadline_before is not None:
        add("deadline_before", f"application deadline before {deadline_before}",
            where="a.application_deadline_dt <= $deadline_before",
            deadline_before=aware(deadline_before))

    # ── who ──────────────────────────────────────────────────────────────
    if bank:
        add("bank", f"bank contains {bank}",
            join="(a)-[:CONDUCTED_BY]->(_bank:Bank)",
            where="toLower(_bank.name) CONTAINS toLower($bank)",
            bank=str(bank).strip())
    if branch:
        add("branch", f"branch contains {branch}",
            join="(a)-[:LISTED_BY_BRANCH]->(_br:Branch)",
            where="toLower(_br.name) CONTAINS toLower($branch)",
            branch=str(branch).strip())
    if borrower:
        add("borrower", f"borrower contains {borrower}",
            join="(a)-[:HAS_BORROWER]->(_bo:Borrower)",
            where="toLower(_bo.name) CONTAINS toLower($borrower)",
            borrower=str(borrower).strip())
    if platform:
        add("platform", f"platform contains {platform}",
            join="(a)-[:HAS_DOCUMENT]->(_pd:Document)-[:HOSTED_ON]->(_pl:Platform)",
            where="toLower(_pl.name) CONTAINS toLower($platform)",
            platform=str(platform).strip())
    if legal_framework:
        lf = require_enum(legal_framework, enums.LEGAL_FRAMEWORKS, "legal_framework")
        add("legal_framework", f"legal framework = {lf}",
            join="(a)-[:HAS_DOCUMENT]->(_fd:Document)-[:UNDER_FRAMEWORK]->(_lf:LegalFramework)",
            where="_lf.name = $legal_framework", legal_framework=lf)

    # ── notice layer ─────────────────────────────────────────────────────
    # Every one of these is EXISTS rather than a join: a notice has several
    # lots, and a join would multiply `a` by its lot count before the DISTINCT,
    # quietly skewing every aggregate computed alongside.
    if area_sqft_min is not None or area_sqft_max is not None:
        lo = float(area_sqft_min) if area_sqft_min is not None else SQFT_FLOOR
        hi = float(area_sqft_max) if area_sqft_max is not None else SQFT_CEIL
        if lo > hi:
            raise ToolInputError(
                f"area_sqft_min ({lo:,.0f}) is above area_sqft_max ({hi:,.0f}).")
        add("area_sqft", f"a lot measuring {lo:,.0f}-{hi:,.0f} sqft",
            where=f"""EXISTS {{ MATCH {_LOT_PATH}-[_e:HAS_EXTENT]->(_m:Measurement)
        WHERE _e.is_headline AND _m.sqft_norm >= $sqft_lo AND _m.sqft_norm <= $sqft_hi
          AND _m.sqft_norm >= $sqft_floor AND _m.sqft_norm <= $sqft_ceil }}""",
            notice_level=True,
            sqft_lo=max(lo, SQFT_FLOOR), sqft_hi=min(hi, SQFT_CEIL),
            sqft_floor=SQFT_FLOOR, sqft_ceil=SQFT_CEIL)
    if possession:
        po = require_enum(possession, enums.POSSESSION_TYPES, "possession")
        add("possession", f"possession = {po}",
            where=f"""EXISTS {{ MATCH {_LOT_PATH}-[:POSSESSION_IS]->(_p:PossessionType)
        WHERE _p.name = $possession }}""",
            notice_level=True, possession=po)
    if road_width_ft_min is not None:
        add("road_width_ft_min", f"road width >= {road_width_ft_min} ft",
            where=f"""EXISTS {{ MATCH {_LOT_PATH}
        WHERE l.road_width_ft >= $road_w
           OR EXISTS {{ MATCH (l)-[:HAS_BOUNDARY]->(_b:Boundary)
                WHERE _b.road_width_ft >= $road_w }} }}""",
            notice_level=True, road_w=float(road_width_ft_min))
    if access_kind:
        ak = require_enum(access_kind, enums.ACCESS_KINDS, "access_kind")
        add("access_kind", f"boundary access = {ak}",
            where=f"""EXISTS {{ MATCH {_LOT_PATH}-[:HAS_BOUNDARY]->(_b:Boundary)
        WHERE _b.access_kind = $access_kind }}""",
            notice_level=True, access_kind=ak)
    if has_encumbrance_note is not None:
        op = "" if has_encumbrance_note else "NOT "
        add("has_encumbrance_note",
            f"notice {'does' if has_encumbrance_note else 'does not'} carry an encumbrance note",
            where=f"""{op}EXISTS {{ MATCH {_LOT_PATH} WHERE l.encumbrance IS NOT NULL }}""",
            notice_level=True)
    if outstanding_max is not None:
        add("outstanding_max", f"secured outstanding <= {outstanding_max:,.0f}",
            where=f"""EXISTS {{ MATCH {_LOT_PATH}-[_s:SECURES]->(:LoanAccount)
        WHERE _s.outstanding_num <= $outstanding_max }}""",
            notice_level=True, outstanding_max=float(outstanding_max))
    if attempt_no is not None:
        add("attempt_no", f"auction attempt #{attempt_no}",
            where=f"""EXISTS {{ MATCH {_LOT_PATH}-[:OFFERED_IN]->(_au:Auction)
        WHERE _au.attempt_no = $attempt_no }}""",
            notice_level=True, attempt_no=int(attempt_no))
    elif reauction_only:
        add("reauction_only", "has failed at least one earlier attempt",
            where=f"""EXISTS {{ MATCH {_LOT_PATH}-[:OFFERED_IN]->(_au:Auction)
        WHERE _au.attempt_no >= 2 }}""",
            notice_level=True)
    if identifier:
        ids = resolve_identifier(identifier, identifier_kind)
        add("identifier", f"identifier {identifier} ({len(ids)} listing(s) mention it)",
            where="a.auction_id IN $identifier_ids", identifier_ids=ids)

    return q


_ROW_PROJECTION = """
WITH DISTINCT a
CALL {
  WITH a
  MATCH (a)-[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->(l:Lot)
  RETURN count(DISTINCT l) AS lot_count
}
CALL {
  WITH a
  MATCH (a)-[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->(l:Lot)
        -[e:HAS_EXTENT]->(m:Measurement)
  WHERE e.is_headline AND m.sqft_norm >= $sqft_floor AND m.sqft_norm <= $sqft_ceil
  RETURN min(m.sqft_norm) AS sqft_min, max(m.sqft_norm) AS sqft_max
}
CALL {
  WITH a
  MATCH (a)-[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->(:Lot)
        -[:OFFERED_IN]->(au:Auction)
  RETURN max(au.attempt_no) AS max_attempt
}
OPTIONAL MATCH (a)-[:CONDUCTED_BY]->(bank:Bank)
OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(city:City)
OPTIONAL MATCH (a)-[:LOCATED_IN_AREA]->(ar:Area)
OPTIONAL MATCH (a)-[:LOCATED_IN_DISTRICT]->(dist:District)
OPTIONAL MATCH (a)-[:HAS_ASSET_CATEGORY]->(ac:AssetCategory)
OPTIONAL MATCH (a)-[:IS_AUCTION_TYPE]->(at:AuctionType)
RETURN a.auction_id AS auction_id,
       a.title AS title,
       city.name AS city, ar.name AS area, dist.name AS district,
       bank.name AS bank,
       ac.name AS asset_category, at.name AS auction_type,
       [(a)-[:HAS_PROPERTY_TYPE]->(pt:PropertyType) | pt.name] AS property_types,
       a.reserve_price_num AS reserve_price, a.emd_num AS emd,
       a.auction_start_dt AS auction_start, a.application_deadline_dt AS deadline,
       a.url AS url,
       lot_count, sqft_min, sqft_max, max_attempt
"""


def _shape_row(r: dict) -> dict:
    """One listing, with its lot-derived values scope-tagged."""
    lot_count = r.get("lot_count") or 0
    scope = scope_of(lot_count)
    lo, hi = r.get("sqft_min"), r.get("sqft_max")
    row = {
        "auction_id": r.get("auction_id"),
        "title": r.get("title"),
        "city": r.get("city"), "area": r.get("area"), "district": r.get("district"),
        "bank": r.get("bank"),
        "asset_category": r.get("asset_category"),
        "property_types": r.get("property_types") or [],
        "auction_type": r.get("auction_type"),
        "reserve_price": r.get("reserve_price"),
        "emd": r.get("emd"),
        "auction_start": json_safe(r.get("auction_start")),
        "application_deadline": json_safe(r.get("deadline")),
        "url": r.get("url"),
        "notice_lot_count": lot_count,
    }
    if lo is not None:
        if scope == "lot":
            row["area_sqft"] = round(float(lo), 1)
            row["area_sqft_scope"] = "lot"
        else:
            row["notice_area_sqft_range"] = [round(float(lo), 1), round(float(hi), 1)]
            row["area_sqft_scope"] = "notice"
    if (r.get("max_attempt") or 0) >= 2:
        row["auction_attempt"] = r["max_attempt"]
        row["attempt_scope"] = scope
    return row


@tool
def find_properties(
    *,
    # place
    city: str | None = None,
    area: str | None = None,
    district: str | None = None,
    taluk: str | None = None,
    revenue_village: str | None = None,
    # what
    asset_category: str | None = None,
    property_type: str | list[str] | None = None,
    auction_type: str | None = None,
    # money, in rupees
    reserve_price_min: float | None = None,
    reserve_price_max: float | None = None,
    emd_min: float | None = None,
    emd_max: float | None = None,
    # time
    auction_from: str | datetime | None = None,
    auction_to: str | datetime | None = None,
    deadline_before: str | datetime | None = None,
    upcoming_only: bool = True,
    # who
    bank: str | None = None,
    branch: str | None = None,
    borrower: str | None = None,
    platform: str | None = None,
    legal_framework: str | None = None,
    # the sale notice
    area_sqft_min: float | None = None,
    area_sqft_max: float | None = None,
    possession: str | None = None,
    road_width_ft_min: float | None = None,
    access_kind: str | None = None,
    has_encumbrance_note: bool | None = None,
    outstanding_max: float | None = None,
    attempt_no: int | None = None,
    reauction_only: bool = False,
    identifier: str | int | None = None,
    identifier_kind: str | None = None,
    # shape
    sort: str = "deadline",
    limit: int = 20,
    group_by: str | None = None,
    sink: ToolSink | None = None,
) -> dict:
    """Search bank-auction listings in Tamil Nadu. One call per question.

    Returns `total_count` (exact, over the whole match set — the panel shows
    all of them) plus up to `limit` rows, `aggregations`, and `refine`:
    narrowings with live counts. On zero rows it returns `relax`, naming the
    single filter to drop and how many matches that unlocks. Read those and
    act on them — do NOT fire a second search to work out how to narrow.

    Prices are rupees: 30 lakhs = 3000000, 1 crore = 10000000.

    `upcoming_only` defaults to True (489 of 2,964 listings are still ahead of
    today). Pass False to search past auctions too.

    Enums, exactly as the graph spells them:
      asset_category   Residential | Commercial | Industrials
                       — this is what "residential"/"commercial" means. NOT
                       property_type.
      property_type    Agricultural Land, Car, Cold Storage Land And Building,
                       Commercial Building, Commercial Property,
                       Commercial Shop, Factory land and Building, Flat,
                       Godown, House, Industrial Land,
                       Industrial Land & Building, Land, Land And Building,
                       Machinary, Non- Agricultural Land, Others,
                       Plant & Machinery, Plot, Residential Unit, Shed,
                       Vehicle, Villa. A list ORs. Common phrasing
                       ("flat", "plot", "shop", "warehouse") is expanded for
                       you.
      auction_type     SARFAESI Auction | DRT Auction | Liquidation Auction |
                       Private Property
      legal_framework  SARFAESI | DRT | IBC | other
      possession       physical | symbolic | constructive
      access_kind      road | street | pathway | plot | channel | setback
      identifier_kind  survey_old, survey_new, patta, plot, door_old,
                       door_new, sale_deed, approved_layout, property_id,
                       flat, assessment_old, assessment_new, block, cersai
      sort             deadline | auction_date | price_asc | price_desc |
                       area_desc | recent
      group_by         city, area, district, taluk, bank, property_type,
                       asset_category, auction_type, platform, price_band,
                       month, attempt_no, possession — returns the breakdown
                       and skips the rows.

    Notice-layer filters — `area_sqft_min/max`, `possession`,
    `road_width_ft_min`, `access_kind`, `has_encumbrance_note`,
    `outstanding_max`, `attempt_no`, `reauction_only` — read the sale notice
    behind the listing. A notice often covers several lots, so these mean "the
    notice contains a lot like this". Any row whose `area_sqft_scope` is
    `notice` must be described that way in your answer, never as this
    property's own measurement. `scope_notes` in the result spells out which
    filters were notice-level.

    `identifier` looks up a survey, patta, door, plot or flat number and finds
    every listing whose notice mentions it.
    """
    if group_by is not None and group_by not in _GROUP_BY:
        raise ToolInputError(f"group_by={group_by!r} is not a dimension this graph has.",
                             valid_values=sorted(_GROUP_BY), field="group_by")
    if sort not in _SORTS:
        raise ToolInputError(f"sort={sort!r} is not a sort this tool supports.",
                             valid_values=sorted(_SORTS), field="sort")
    limit = clamp_limit(limit)

    q = _build(
        city=city, area=area, district=district, taluk=taluk,
        revenue_village=revenue_village, asset_category=asset_category,
        property_type=property_type, auction_type=auction_type,
        reserve_price_min=reserve_price_min, reserve_price_max=reserve_price_max,
        emd_min=emd_min, emd_max=emd_max, auction_from=auction_from,
        auction_to=auction_to, deadline_before=deadline_before,
        upcoming_only=upcoming_only, bank=bank, branch=branch, borrower=borrower,
        platform=platform, legal_framework=legal_framework,
        area_sqft_min=area_sqft_min, area_sqft_max=area_sqft_max,
        possession=possession, road_width_ft_min=road_width_ft_min,
        access_kind=access_kind, has_encumbrance_note=has_encumbrance_note,
        outstanding_max=outstanding_max, attempt_no=attempt_no,
        reauction_only=reauction_only, identifier=identifier,
        identifier_kind=identifier_kind,
    )
    base = q.base()
    params = dict(q.params)
    params.update({"sqft_floor": SQFT_FLOOR, "sqft_ceil": SQFT_CEIL})

    # ── count + aggregates, one query ────────────────────────────────────
    agg_rows = run_read_query(f"""
    {base}
    WITH DISTINCT a
    RETURN count(a) AS total_count,
           min(a.reserve_price_num) AS reserve_min,
           max(a.reserve_price_num) AS reserve_max,
           avg(a.reserve_price_num) AS reserve_avg,
           count(a.reserve_price_num) AS reserve_known
    """, params, timeout=20.0, max_rows=1)
    agg = agg_rows[0] if agg_rows else {}
    total = int(agg.get("total_count") or 0)

    out: dict = {
        "total_count": total,
        "filters_applied": [d for _, d in q.active],
    }
    if q.notice_level:
        out["scope_notes"] = [
            f"`{f}` matched the sale notice, which may cover several lots — "
            f"it means the notice contains a lot like this, not that this "
            f"listing is that lot." for f in q.notice_level
        ]

    if total:
        out["aggregations"] = {
            "reserve_price_min": agg.get("reserve_min"),
            "reserve_price_max": agg.get("reserve_max"),
            "reserve_price_avg": (round(float(agg["reserve_avg"]), 0)
                                  if agg.get("reserve_avg") is not None else None),
            "listings_with_reserve_price": agg.get("reserve_known"),
        }

    # ── breakdown mode: the buckets are the answer, skip the rows ────────
    if group_by:
        out["group_by"] = group_by
        out["distribution"] = _distribution(base, params, group_by)
        out["rows"] = []
        return out

    if not total:
        out["rows"] = []
        out["relax"] = _relax(q, params)
        out["hint"] = _zero_hint(q, out["relax"], upcoming_only)
        return out

    # ── rows ─────────────────────────────────────────────────────────────
    # With a sink the panel wants every match it can hold, not the model's
    # slice — fetching `limit` here would leave the panel showing 5 rows while
    # the answer talks about 200.
    fetch = PANEL_ROW_CAP if sink is not None else limit
    row_params = dict(params)
    row_params["row_limit"] = fetch
    raw = run_read_query(
        f"{base}\n{_ROW_PROJECTION}\nORDER BY {_SORTS[sort]}\nLIMIT $row_limit",
        row_params, timeout=25.0, max_rows=fetch)
    shaped = [_shape_row(r) for r in raw]
    if sink is not None:
        sink.absorb(shaped)
    out["rows"] = shaped[:limit]
    out["rows_shown"] = len(out["rows"])
    out["sort"] = sort

    if total > len(out["rows"]):
        out["refine"] = _refine(base, params)
        out["note"] = (
            f"Showing {len(out['rows'])} of {total}. The counts and "
            f"aggregations above are exact over all {total}; the panel shows "
            f"every match. Use `refine` to narrow — do not re-search.")
    return out


def _distribution(base: str, params: dict, dimension: str) -> list[dict]:
    join, value_expr = _GROUP_BY[dimension]
    match = f"MATCH {join}" if join else ""
    rows = run_read_query(f"""
    {base}
    WITH DISTINCT a
    {match}
    WITH {value_expr} AS value, count(DISTINCT a) AS listings
    WHERE value IS NOT NULL
    RETURN value, listings ORDER BY listings DESC, value ASC LIMIT 40
    """, params, timeout=25.0, max_rows=40)
    return [{"value": r["value"], "listings": r["listings"]} for r in rows]


#: The dimensions worth offering as a narrowing, cheapest and most useful
#: first. Kept short on purpose: three good options beat ten.
_REFINE_DIMS = ("area", "property_type", "price_band")


def _refine(base: str, params: dict) -> list[dict]:
    """Live, guaranteed non-empty narrowings, in one round trip.

    Each branch re-states the base query. That looks wasteful and is not:
    the planner runs each against the same indexes, and the alternative — one
    query per dimension — is three round trips, or worse, an extra model call
    to work out how to narrow.
    """
    branches = []
    for dim in _REFINE_DIMS:
        join, value_expr = _GROUP_BY[dim]
        match = f"MATCH {join}" if join else ""
        branches.append(f"""
        {base}
        WITH DISTINCT a
        {match}
        WITH '{dim}' AS dimension, {value_expr} AS value, count(DISTINCT a) AS listings
        WHERE value IS NOT NULL
        RETURN dimension, value, listings ORDER BY listings DESC LIMIT 3
        """)
    rows = run_read_query("\nUNION ALL\n".join(branches), params,
                          timeout=25.0, max_rows=20)
    return [{"filter": r["dimension"], "value": r["value"], "listings": r["listings"]}
            for r in rows]


def _relax(q: _Query, params: dict) -> list[dict]:
    """On zero rows: which single filter to drop, and what it unlocks.

    One branch per active filter, one round trip. Only filters that actually
    unlock something are returned — an option that leads to another zero is
    noise, not help.
    """
    labels = [lbl for lbl, _, _ in q._fragments]
    if len(labels) < 2:
        return []
    branches = []
    for label in labels:
        sub_base, _ = q.base_without(label)
        branches.append(f"""
        {sub_base}
        WITH DISTINCT a
        RETURN '{label}' AS dropped, count(a) AS listings
        """)
    rows = run_read_query("\nUNION ALL\n".join(branches), params,
                          timeout=25.0, max_rows=len(labels))
    desc = dict(q.active)
    return sorted(
        [{"drop_filter": r["dropped"], "was": desc.get(r["dropped"], r["dropped"]),
          "listings_if_dropped": r["listings"]}
         for r in rows if (r["listings"] or 0) > 0],
        key=lambda d: d["listings_if_dropped"], reverse=True)


def _zero_hint(q: _Query, relax: list[dict], upcoming_only: bool) -> str:
    if relax:
        top = relax[0]
        return (f"No listing matches all {len(q.active)} filters. Dropping "
                f"`{top['drop_filter']}` ({top['was']}) would match "
                f"{top['listings_if_dropped']}. Ask the user which constraint "
                f"to loosen rather than guessing.")
    if upcoming_only:
        return ("Nothing upcoming matches. Only 489 of 2,964 listings are "
                "still ahead of today — pass upcoming_only=False to include "
                "auctions that have already run.")
    return ("Nothing in the graph matches these filters. Report that plainly; "
            "do not loosen filters to manufacture a match.")
