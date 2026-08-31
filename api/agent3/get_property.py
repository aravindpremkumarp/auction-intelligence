"""
api/agent3/get_property.py
--------------------------
The diligence tool. Everything known about a listing, in one call.

Two things it does that no current tool does:

**It reads the sale notice.** Schedule text, extent by kind, survey and patta
numbers, boundaries side by side with road width and access, possession and
when it was taken, the encumbrance clause, secured outstanding and demand
notice date, parties by role, the EMD account and IFSC, the signing officer,
and the notice PDF.

**It reports what the notice does NOT say.** `gaps` is a named list of
omissions — no patta number, possession not stated, no encumbrance clause.
For a buyer the gaps are the product: an answer that only recites what is
present makes an incomplete notice look clean, which is the expensive kind of
wrong.

Scope discipline is enforced here rather than left to the prompt. Every
lot-derived value is returned under a `scope` of `lot` (single-lot notice —
this is this property) or `notice` (multi-lot — this is context), and on a
multi-lot notice the per-lot detail is returned as `notice_lots`, a list,
never as flat property fields. There is no shape in this return value that
lets the agent state a six-lot notice's extent as the property's own.
"""
from __future__ import annotations

from api.agent3.common import (
    MAX_DETAIL_IDS, SQFT_CEIL, SQFT_FLOOR, ToolInputError, band_note,
    json_safe, scope_note, scope_of, tool,
)
from api.neo4j_client import run_read_query

#: Lot prose is the longest thing in this payload. Full notice text belongs in
#: the document viewer, not in a transcript that is re-sent every turn.
DESCRIPTION_CHARS = 1500

_LISTING_CYPHER = """
UNWIND $ids AS aid
MATCH (a:AuctionProperty {auction_id: aid})
OPTIONAL MATCH (a)-[:CONDUCTED_BY]->(bank:Bank)
OPTIONAL MATCH (a)-[:LISTED_BY_BRANCH]->(br:Branch)
OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(city:City)
OPTIONAL MATCH (a)-[:LOCATED_IN_AREA]->(ar:Area)
OPTIONAL MATCH (a)-[:LOCATED_IN_DISTRICT]->(dist:District)
OPTIONAL MATCH (a)-[:LOCATED_IN_TALUK]->(tal:Taluk)
OPTIONAL MATCH (a)-[:LOCATED_IN_REVENUE_VILLAGE]->(rv:RevenueVillage)
OPTIONAL MATCH (a)-[:HAS_ASSET_CATEGORY]->(ac:AssetCategory)
OPTIONAL MATCH (a)-[:IS_AUCTION_TYPE]->(at:AuctionType)
RETURN a.auction_id AS auction_id, a.title AS title, a.url AS url,
       left(a.description, $desc_chars) AS description,
       size(coalesce(a.description, '')) AS description_len,
       a.reserve_price_num AS reserve_price, a.reserve_price_raw AS reserve_price_raw,
       a.emd_num AS emd, a.emd_raw AS emd_raw,
       a.auction_start_dt AS auction_start, a.auction_end_dt AS auction_end,
       a.application_deadline_dt AS application_deadline,
       a.contact_details AS contact_details, a.service_provider AS service_provider,
       a.total_area AS total_area_raw, a.undivided_share AS undivided_share,
       a.door_numbers_new AS door_new, a.door_numbers_old AS door_old,
       a.registration_sub_district AS sro,
       bank.name AS bank, br.name AS branch, city.name AS city, ar.name AS area,
       dist.name AS district, tal.name AS taluk, rv.name AS revenue_village,
       ac.name AS asset_category, at.name AS auction_type,
       [(a)-[:HAS_PROPERTY_TYPE]->(pt:PropertyType) | pt.name] AS property_types,
       [(a)-[:HAS_BORROWER]->(b:Borrower) | b.name] AS borrowers,
       [(a)-[:SAME_PROPERTY_AS]->(o:AuctionProperty) | o.auction_id] AS same_property_as,
       // Phase 2: the lot comes from the edge, not the string beside it. A
       // key is "<filename>#<lot_index>" and lot_index is the model's own
       // numbering, so a re-extraction renumbers the lots and a stale key
       // still RESOLVES — to a different property. The edge names the node.
       [(a)-[:IS_LOT]->(_lot:Lot) | _lot.lot_key][0] AS resolved_lot_key
"""

_DOCUMENT_CYPHER = """
UNWIND $ids AS aid
MATCH (a:AuctionProperty {auction_id: aid})-[:HAS_DOCUMENT]->(d:Document)
OPTIONAL MATCH (d)-[:HOSTED_ON]->(pl:Platform)
OPTIONAL MATCH (d)-[:UNDER_FRAMEWORK]->(lf:LegalFramework)
OPTIONAL MATCH (d)-[:ISSUED_BY]->(ib:Bank)
RETURN aid AS auction_id, d.public_url AS notice_url, d.filename AS filename,
       d.notice_type AS notice_type, d.doc_type AS doc_type,
       left(d.sale_terms, $desc_chars) AS sale_terms,
       d.parse_quality_score AS parse_quality,
       pl.name AS platform, lf.name AS legal_framework, ib.name AS issuing_bank,
       [(d)-[:EMD_PAYABLE_TO]->(e:EMDAccount) |
          {account_name: e.account_name, account_no: e.account_no,
           ifsc: e.ifsc, mode_of_payment: e.mode_of_payment}] AS emd_accounts,
       [(d)-[:HAS_CONTACT]->(c:Contact) |
          {phone: c.phone, email: c.email}] AS contacts,
       [(d)-[s:SIGNED_BY]->(o:Officer) | {name: o.name, role: s.role}] AS officers,
       [(d)-[:CASE_REF]->(cr:CaseReference) | cr.ref] AS case_references,
       [(d)-[:UNDER_TRUST]->(t:Trust) | t.name] AS trusts
"""

_LOTS_CYPHER = """
UNWIND $ids AS aid
MATCH (a:AuctionProperty {auction_id: aid})-[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->(l:Lot)
OPTIONAL MATCH (l)-[pr:POSSESSION_IS]->(pt:PossessionType)
RETURN aid AS auction_id, l.lot_key AS lot_key, l.lot_index AS lot_index,
       l.address AS address, l.village AS village, l.taluk AS taluk,
       l.district AS district, l.asset_category AS asset_category,
       l.property_type AS property_type, l.encumbrance AS encumbrance,
       l.road_width_ft AS road_width_ft, l.frontage_ft AS frontage_ft,
       l.construction_type AS construction_type,
       l.occupancy_status AS occupancy_status, l.landmark AS landmark,
       left(l.full_description, $desc_chars) AS full_description,
       pt.name AS possession_type, pr.taken_on AS possession_taken_on,
       [(l)-[e:HAS_EXTENT]->(m:Measurement) |
          {kind: e.kind, is_headline: e.is_headline, sqft: m.sqft_norm,
           raw: m.raw, unit: m.unit}] AS extents,
       [(l)-[:MENTIONS_IDENTIFIER]->(i:Identifier) |
          {kind: i.kind, value: i.value_raw}] AS identifiers,
       [(l)-[:HAS_BOUNDARY]->(b:Boundary) |
          {side: b.side, adjacent: b.adjacency_raw, length_ft: b.measurement_ft,
           road_width_ft: b.road_width_ft, access_kind: b.access_kind}] AS boundaries,
       [(l)-[s:SECURES]->(la:LoanAccount) |
          {account_no: la.account_no, outstanding: s.outstanding_num,
           demand_notice_date: s.demand_notice_date, as_on: s.as_on}] AS loans,
       [(l)-[hp:HAS_PARTY]->(b:Borrower) | {name: b.name, role: hp.role}] AS parties,
       [(l)-[:TITLE_HELD_BY]->(b:Borrower) | b.name] AS title_holders,
       [(l)-[:HAS_SCHEDULE]->(sc:Schedule) |
          {label: sc.label, type: sc.type, extent: sc.extent}] AS schedules,
       [(l)-[:OFFERED_IN]->(au:Auction) |
          {attempt_no: au.attempt_no, reserve_price: au.reserve_price_num,
           emd: au.emd_num, bid_increment: au.bid_increment_num,
           auction_start: au.auction_start_dt, auction_end: au.auction_end_dt,
           inspection: au.inspection_dt, application_deadline: au.application_deadline_dt,
           sarfaesi_stage: au.sarfaesi_stage,
           auto_extension_minutes: au.auto_extension_minutes}] AS auctions
"""


def _headline_sqft(extents: list[dict]) -> tuple[float | None, int]:
    """The one extent that describes the lot, plus how many were out of band.

    Prefers `is_headline`. Never falls back to `uds`/`uds_parent`: a flat's
    undivided share of the parent plot is not its area, and echoing the parent
    extent as the property's own turns a 760 sqft flat into a 2,257 sqft one.
    """
    excluded = 0
    headline: float | None = None
    fallback: float | None = None
    for e in extents or []:
        sqft = e.get("sqft")
        if sqft is None:
            continue
        if not (SQFT_FLOOR <= float(sqft) <= SQFT_CEIL):
            excluded += 1
            continue
        if e.get("is_headline"):
            headline = float(sqft)
        elif e.get("kind") in ("extent", "total") and fallback is None:
            fallback = float(sqft)
    return (headline if headline is not None else fallback), excluded


def _clean_lot(row: dict) -> dict:
    """One lot, trimmed to what a buyer question actually needs."""
    sqft, excluded = _headline_sqft(row.get("extents") or [])
    lot = {
        "lot_key": row.get("lot_key"),
        "lot_index": row.get("lot_index"),
        "property_type": row.get("property_type"),
        "asset_category": row.get("asset_category"),
        "address": row.get("address"),
        "village": row.get("village"), "taluk": row.get("taluk"),
        "district": row.get("district"),
        "headline_sqft": round(sqft, 1) if sqft is not None else None,
        "extents": [e for e in (row.get("extents") or []) if e.get("sqft") is not None],
        "identifiers": row.get("identifiers") or [],
        "boundaries": row.get("boundaries") or [],
        "possession": ({"type": row["possession_type"],
                        "taken_on": row.get("possession_taken_on")}
                       if row.get("possession_type") else None),
        "encumbrance": row.get("encumbrance"),
        "road_width_ft": row.get("road_width_ft"),
        "frontage_ft": row.get("frontage_ft"),
        "construction_type": row.get("construction_type"),
        "occupancy_status": row.get("occupancy_status"),
        "loans": row.get("loans") or [],
        "parties": row.get("parties") or [],
        "title_holders": row.get("title_holders") or [],
        "schedules": row.get("schedules") or [],
        "auctions": json_safe(row.get("auctions") or []),
        "description": row.get("full_description"),
    }
    warning = band_note(excluded)
    if warning:
        lot["extent_warning"] = warning
    return {k: v for k, v in lot.items() if v not in (None, [], {})}


def _identifier_kinds(lots: list[dict]) -> set[str]:
    return {i.get("kind") for lot in lots for i in lot.get("identifiers", [])
            if i.get("kind")}


def _gaps(listing: dict, doc: dict, lots: list[dict]) -> list[str]:
    """What the notice does not say. The diligence product.

    Every entry is a thing a buyer would otherwise assume was checked. Ordered
    by how much it should change their behaviour.
    """
    gaps: list[str] = []
    if not lots:
        gaps.append("No sale-notice lot could be read for this listing — "
                    "every fact below comes from the portal row alone.")
        return gaps

    kinds = _identifier_kinds(lots)
    if not any(k.startswith("survey") for k in kinds):
        gaps.append("No survey number in the notice — the land cannot be "
                    "identified against revenue records from this listing.")
    if "patta" not in kinds:
        gaps.append("No patta number in the notice.")
    if not any(lot.get("possession") for lot in lots):
        gaps.append("Possession is not stated — the notice does not say "
                    "whether the bank holds physical or only symbolic "
                    "possession.")
    if not any(lot.get("encumbrance") for lot in lots):
        gaps.append("No encumbrance clause in the notice — read it as "
                    "unstated, not as 'no encumbrance'.")
    if not any(lot.get("boundaries") for lot in lots):
        gaps.append("No boundary schedule — the extent cannot be checked "
                    "against the four sides.")
    if not any(lot.get("headline_sqft") for lot in lots):
        gaps.append("No usable extent — the notice gives no area this tool "
                    "could normalise to square feet.")
    if not any(a.get("inspection") for lot in lots for a in lot.get("auctions", [])):
        gaps.append("No inspection date given.")
    if not any(e.get("ifsc") for e in doc.get("emd_accounts") or []):
        gaps.append("No EMD account or IFSC in the notice — EMD payment "
                    "details must come from the platform.")
    if not (doc.get("contacts") or listing.get("contact_details")):
        gaps.append("No contact phone or email on the notice.")
    if listing.get("reserve_price") is None:
        gaps.append("No reserve price on the listing.")
    if not doc.get("notice_url"):
        gaps.append("The original notice file is not linked.")
    return gaps


@tool
def get_property(auction_ids: str | int | list[str | int],
                 depth: str = "standard") -> dict:
    """Everything known about one listing (or up to 5), from listing and notice.

    `depth="standard"` — the listing, bank, branch, platform, dates, EMD,
    contacts, and a summary of the notice's lots.
    `depth="full"` — adds every lot: schedule, extent by kind, survey/patta
    numbers, boundaries with road width and access, possession and when it was
    taken, the encumbrance clause, secured outstanding, parties by role, and
    the per-attempt auction terms.

    Always returns `gaps` — what the notice does NOT say. Report those; an
    answer that lists only what is present makes an incomplete notice look
    clean.

    **Scope.** A sale notice often covers several lots and does not say which
    one this listing is. When it covers exactly one, lot facts appear under
    `property` with `"scope": "lot"` and are this property's own. When it
    covers more, they appear under `notice_lots` with `"scope": "notice"` —
    describe them as "the notice covers N lots, ranging ..." and never pick
    one and present it as this property's size, survey number or possession
    status. `scope_note` gives you the sentence to use.

    This graph has NO sold prices and no market valuation — `Auction.outcome`
    is never populated beyond "unsold". "Did it sell" and "what did it fetch"
    have no answer here.
    """
    if depth not in ("standard", "full"):
        raise ToolInputError(f"depth={depth!r} is not supported.",
                             valid_values=("standard", "full"), field="depth")
    # An auction_id looks like a number ("744314"), so a model will send one
    # as an int about half the time. The type hint accepts that on purpose:
    # pydantic validates the tool schema BEFORE our error-as-data decorator
    # runs, so a str-only hint produces a raw framework rejection the model
    # cannot learn from. Observed in the first real-model smoke run — it
    # burned three of six model calls retrying `auction_ids: 744314`
    # verbatim before guessing the list-of-str form.
    if isinstance(auction_ids, (str, int)):
        auction_ids = [auction_ids]
    ids = [str(x).strip() for x in (auction_ids or []) if str(x).strip()]
    if not ids:
        raise ToolInputError("auction_ids is empty — pass at least one auction_id.")
    if len(ids) > MAX_DETAIL_IDS:
        raise ToolInputError(
            f"{len(ids)} ids requested; {MAX_DETAIL_IDS} is the cap. Ask for the "
            f"most relevant {MAX_DETAIL_IDS} — a full dossier is a large payload.")

    params = {"ids": ids, "desc_chars": DESCRIPTION_CHARS}
    listings = run_read_query(_LISTING_CYPHER, params, timeout=20.0, max_rows=MAX_DETAIL_IDS)
    if not listings:
        return {"properties": [], "not_found": ids,
                "hint": ("No listing carries these auction_ids. They come from "
                         "find_properties results — do not guess one.")}

    docs = run_read_query(_DOCUMENT_CYPHER, params, timeout=20.0, max_rows=MAX_DETAIL_IDS * 3)
    lot_rows = run_read_query(_LOTS_CYPHER, params, timeout=30.0, max_rows=200)

    by_doc: dict[str, dict] = {}
    for d in docs:
        by_doc.setdefault(d["auction_id"], json_safe(d))
    by_lots: dict[str, list[dict]] = {}
    for r in lot_rows:
        by_lots.setdefault(r["auction_id"], []).append(_clean_lot(json_safe(r)))

    out_props = []
    for raw in listings:
        listing = json_safe(raw)
        aid = listing["auction_id"]
        doc = by_doc.get(aid, {})
        lots = by_lots.get(aid, [])
        lot_count = len(lots)
        resolved = bool(listing.get("resolved_lot_key"))
        scope = scope_of(lot_count, resolved)

        prop: dict = {
            "auction_id": aid,
            "scope": scope,
            "notice_lot_count": lot_count,
            "listing": {k: v for k, v in listing.items()
                        if k not in ("auction_id", "resolved_lot_key")
                        and v not in (None, [], "")},
            "notice": {k: v for k, v in doc.items()
                       if k != "auction_id" and v not in (None, [], "")},
            "gaps": _gaps(listing, doc, lots),
        }
        note = scope_note("the notice detail below", lot_count, resolved)
        if note:
            prop["scope_note"] = note

        if scope == "lot" and lots:
            # Single-lot notice: `lots[0]` IS the property, by construction.
            # A resolved multi-lot notice has several lots in `lots` and
            # must pick the SPECIFIC one the resolver named — never assume
            # list order lines up with `resolved_lot_key`.
            own_lot = lots[0]
            if resolved and lot_count > 1:
                key = listing.get("resolved_lot_key")
                own_lot = next((x for x in lots if x.get("lot_key") == key),
                               lots[0])
            prop["property"] = own_lot
            if depth == "standard":
                prop["property"].pop("description", None)
                prop["property"].pop("schedules", None)
        elif lots:
            # Multi-lot notice: a list, never flattened. The shape itself is
            # what stops "this property is 2,400 sqft" being written.
            sizes = [x["headline_sqft"] for x in lots if x.get("headline_sqft")]
            prop["notice_summary"] = {
                "lot_count": lot_count,
                "sqft_range": ([min(sizes), max(sizes)] if sizes else None),
                "possession_types": sorted({x["possession"]["type"] for x in lots
                                            if x.get("possession")}),
                "lots_with_encumbrance_note": sum(1 for x in lots if x.get("encumbrance")),
                "property_types": sorted({x["property_type"] for x in lots
                                          if x.get("property_type")}),
            }
            if depth == "full":
                prop["notice_lots"] = lots
            else:
                prop["notice_lots_hint"] = (
                    f"Call again with depth='full' for all {lot_count} lots.")
        out_props.append(prop)

    result: dict = {"properties": out_props}
    missing = [i for i in ids if i not in {p["auction_id"] for p in out_props}]
    if missing:
        result["not_found"] = missing
    return result
