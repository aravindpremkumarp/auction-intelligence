"""
api/dossier/taxonomy.py
-----------------------
The single source of truth for the property-diligence document taxonomy.

This module is intentionally **pure data + pure functions** (no Neo4j, no
network, no heavy imports) so it can be shared by the API router, the
document-type classifier in ``pipeline/classify_document.py``, and the unit
tests without dragging in app state.

Two artefacts, straight from the design doc (docs/design/2026-06-13-…):

* ``CATEGORIES`` / ``DOC_TYPES`` — the 9 diligence categories and the ~50
  document types under them. ``DOC_TYPES`` doubles as the literal label set the
  classifier predicts into and the source of the full checklist.
* ``MINIMUM_SET`` — the 12-item "absolute minimum I would not skip" subset that
  drives the Diligence Readiness Score (Appendix B).

Stable string ``id``s are the contract: the classifier emits them, the graph
stores them, the frontend renders them. Renaming an ``id`` is a migration —
add new ones, don't rename.
"""
from __future__ import annotations

from typing import Iterable, NamedTuple


# ── Categories (Appendix A: the 9 diligence categories, A–I) ─────────────────

class Category(NamedTuple):
    id: str          # single-letter code, matches the design doc
    label: str


CATEGORIES: tuple[Category, ...] = (
    Category("A", "Documents from the Bank"),
    Category("B", "Title Documents"),
    Category("C", "Revenue Records"),
    Category("D", "Encumbrance & Registration Records"),
    Category("E", "Layout & Approval Documents"),
    Category("F", "Tax & Utility Documents"),
    Category("G", "Apartment-Specific Documents"),
    Category("H", "Legal Verification Documents"),
    Category("I", "Documents Received After Winning Auction"),
)

CATEGORY_LABELS: dict[str, str] = {c.id: c.label for c in CATEGORIES}


# ── Document types (Appendix A, full ~50-type taxonomy) ──────────────────────

class DocType(NamedTuple):
    id: str             # stable slug — the classifier label + graph value
    label: str
    category: str       # Category.id
    conditional: bool   # True for "(if applicable)" rows


def _d(id: str, label: str, category: str, conditional: bool = False) -> DocType:
    return DocType(id, label, category, conditional)


DOC_TYPES: tuple[DocType, ...] = (
    # A. Documents from the Bank
    _d("auction_notice",          "Auction Notice",                          "A"),
    _d("sale_notice",             "Sale Notice",                             "A"),
    _d("possession_notice",       "Possession Notice",                       "A"),
    _d("demand_notice",           "Demand Notice (SARFAESI)",                "A"),
    _d("sale_terms",              "Sale Terms & Conditions",                 "A"),
    _d("inspection_report",       "Property Inspection Report",              "A", True),
    _d("bank_valuation_report",   "Valuation Report (bank)",                 "A", True),
    _d("mortgage_deed_modt",      "Mortgage Deed / MODT",                    "A"),
    _d("list_of_original_docs",   "List of Original Documents held by Bank", "A"),

    # B. Title Documents
    _d("sale_deed",               "Latest Sale Deed",                        "B"),
    _d("mother_deed",             "Mother Deed (30–40 yr chain)",            "B"),
    _d("previous_title_deeds",    "Previous Title Deeds",                    "B"),
    _d("settlement_deed",         "Settlement Deed",                         "B", True),
    _d("gift_deed",               "Gift Deed",                               "B", True),
    _d("partition_deed",          "Partition Deed",                          "B", True),
    _d("release_deed",            "Release Deed",                            "B", True),
    _d("power_of_attorney",       "Power of Attorney",                       "B", True),
    _d("legal_heir_certificate",  "Legal Heir Certificate",                  "B", True),
    _d("death_certificate",       "Death Certificate",                       "B", True),

    # C. Revenue Records
    _d("patta",                   "Patta",                                   "C"),
    _d("chitta",                  "Chitta",                                  "C"),
    _d("a_register",              "A-Register Extract",                      "C"),
    _d("fmb_sketch",              "FMB Sketch (Field Measurement Book)",     "C"),
    _d("tslr_extract",            "TSLR Extract",                            "C", True),

    # D. Encumbrance & Registration Records
    _d("encumbrance_certificate", "Encumbrance Certificate (EC)",            "D"),
    _d("mortgage_registration",   "Mortgage Registration Details",          "D"),
    _d("mortgage_release",        "Mortgage Release Documents",              "D", True),

    # E. Layout & Approval Documents
    _d("dtcp_cmda_layout",        "DTCP / CMDA Approved Layout",             "E"),
    _d("layout_approval_order",   "Layout Approval Order",                   "E"),
    _d("building_plan_approval",  "Building Plan Approval",                  "E"),
    _d("planning_permission",     "Planning Permission",                     "E"),
    _d("completion_certificate",  "Completion Certificate (CC)",            "E"),
    _d("occupancy_certificate",   "Occupancy Certificate (OC)",            "E"),
    _d("rera_registration",       "RERA Registration Details",              "E", True),

    # F. Tax & Utility Documents
    _d("property_tax_receipt",    "Property Tax Receipts",                  "F"),
    _d("water_tax_receipt",       "Water Tax Receipts",                     "F"),
    _d("electricity_bill",        "Electricity Bills",                      "F"),
    _d("sewerage_receipt",        "Sewerage Charges Receipts",              "F"),
    _d("betterment_charges",      "Betterment Charges Receipt",             "F", True),

    # G. Apartment-Specific Documents
    _d("uds_details",             "Undivided Share (UDS) Details",          "G"),
    _d("association_no_due",      "Apartment Association No-Due Certificate", "G"),
    _d("maintenance_due_statement", "Maintenance Due Statement",            "G"),
    _d("car_parking_allocation",  "Car Parking Allocation Documents",       "G"),
    _d("builder_buyer_agreement", "Builder-Buyer Agreement",                "G", True),

    # H. Legal Verification Documents
    _d("advocate_legal_opinion",  "Independent Advocate Legal Opinion",     "H"),
    _d("title_search_report",     "Title Search Report",                    "H"),
    _d("court_case_search_report", "Court Case Search Report",              "H"),
    _d("litigation_search_report", "Litigation Search Report",              "H", True),

    # I. Documents Received After Winning Auction
    _d("bid_acceptance_letter",   "Bid Acceptance Letter",                  "I"),
    _d("sale_confirmation_letter", "Sale Confirmation Letter",              "I"),
    _d("sale_certificate",        "Sale Certificate",                       "I"),
    _d("possession_letter",       "Possession Letter / Memo",               "I"),
    _d("original_docs_released",   "Original Title Documents released by Bank", "I"),
    _d("handing_over_memo",       "Handing Over Memo",                      "I"),
)

# Sentinel the classifier may return when it cannot confidently place a doc.
# Never counts toward the checklist; surfaced to the user as "unclassified".
UNKNOWN_DOC_TYPE = "unknown"

DOC_TYPE_BY_ID: dict[str, DocType] = {d.id: d for d in DOC_TYPES}
ALL_DOC_TYPE_IDS: frozenset[str] = frozenset(DOC_TYPE_BY_ID)
DOC_TYPE_TO_CATEGORY: dict[str, str] = {d.id: d.category for d in DOC_TYPES}


# ── Minimum set (Appendix B: drives the readiness score) ─────────────────────

class MinimumItem(NamedTuple):
    label: str
    # A minimum item is satisfied when ANY of these doc types is present.
    # Most map 1:1; item 12 (layout/building approval) is satisfied by any of
    # several approval doc types.
    doc_type_ids: tuple[str, ...]
    # True for items 1–9 & 12 the user can actually upload today; these are the
    # 10 items the score is computed over. Items 10 & 11 are Phase-2 *outputs*
    # the locker will help produce — advisory rows that never subtract.
    uploadable: bool


MINIMUM_SET: tuple[MinimumItem, ...] = (
    MinimumItem("Sale Deed",                     ("sale_deed",),               True),
    MinimumItem("Mother Deed",                   ("mother_deed",),             True),
    MinimumItem("Encumbrance Certificate",       ("encumbrance_certificate",), True),
    MinimumItem("Patta",                         ("patta",),                   True),
    MinimumItem("FMB Sketch",                    ("fmb_sketch",),              True),
    MinimumItem("Auction Notice",                ("auction_notice",),          True),
    MinimumItem("Possession Notice",             ("possession_notice",),       True),
    MinimumItem("Mortgage Document / MODT",      ("mortgage_deed_modt",),      True),
    MinimumItem("Property Tax Receipt",          ("property_tax_receipt",),    True),
    MinimumItem("Advocate Legal Opinion",        ("advocate_legal_opinion",),  False),
    MinimumItem("Court Case Search Report",      ("court_case_search_report",), False),
    MinimumItem(
        "Layout / Building Approval",
        ("dtcp_cmda_layout", "layout_approval_order",
         "building_plan_approval", "planning_permission"),
        True,
    ),
)

# The score denominator: count of uploadable minimum items (10 in v1).
SCORABLE_ITEM_COUNT: int = sum(1 for m in MINIMUM_SET if m.uploadable)


# ── "Go get it" portal deep-links (v1 = bare deep-links, no pre-fill) ────────
#
# Open Question 4 in the design doc flags that these portals change and that
# URL-param pre-fill may not be possible — so v1 ships the safe version
# (deep-link + a human "how to get it" note) and keeps everything in this one
# config block so a broken link is a one-line fix, not a code hunt.

PORTALS: dict[str, str] = {
    "tnreginet":    "https://tnreginet.gov.in/portal/",
    "tn_eservices": "https://eservices.tn.gov.in/eservicesnew/home.html",
}


class PortalLink(NamedTuple):
    portal: str        # human label, e.g. "TNREGINET"
    url: str
    how: str           # one-line instruction on where/how to obtain it


# Per-doc-type guidance for the most important docs. Anything not listed falls
# back to a per-category hint via :func:`portal_link_for`.
_DOC_TYPE_PORTAL: dict[str, PortalLink] = {
    "encumbrance_certificate": PortalLink(
        "TNREGINET", PORTALS["tnreginet"],
        "Apply for the Encumbrance Certificate (EC) under E-Services → "
        "Encumbrance Certificate; request the full 30-year period.",
    ),
    "patta": PortalLink(
        "TN e-Services", PORTALS["tn_eservices"],
        "Get the Patta/Chitta extract under Revenue → Patta & Chitta using the "
        "survey number and village.",
    ),
    "chitta": PortalLink(
        "TN e-Services", PORTALS["tn_eservices"],
        "Get the Chitta under Revenue → Patta & Chitta using the survey number.",
    ),
    "a_register": PortalLink(
        "TN e-Services", PORTALS["tn_eservices"],
        "Request the A-Register extract from the Village Administrative Officer "
        "or via the Revenue e-Services portal.",
    ),
    "fmb_sketch": PortalLink(
        "TN e-Services", PORTALS["tn_eservices"],
        "Get the FMB (Field Measurement Book) sketch under Revenue → FMB using "
        "the survey number.",
    ),
    "tslr_extract": PortalLink(
        "TN e-Services", PORTALS["tn_eservices"],
        "Request the TSLR extract (urban survey) from the Survey & Settlement "
        "office for the locality.",
    ),
}

# Per-category fallback for docs without a single online portal.
_CATEGORY_PORTAL: dict[str, PortalLink] = {
    "A": PortalLink(
        "Lender / Bank", "",
        "Obtain from the bank's authorised officer or the published auction "
        "notice (the same SARFAESI sale notice AuctionScope ingests).",
    ),
    "B": PortalLink(
        "Sub-Registrar / TNREGINET", PORTALS["tnreginet"],
        "Get certified copies of the deed chain from the Sub-Registrar Office "
        "or via TNREGINET document search.",
    ),
    "C": PortalLink(
        "TN e-Services", PORTALS["tn_eservices"],
        "Obtain the revenue record from the Tamil Nadu Revenue e-Services "
        "portal using the survey number.",
    ),
    "D": PortalLink(
        "TNREGINET", PORTALS["tnreginet"],
        "Search registration/encumbrance records on TNREGINET.",
    ),
    "E": PortalLink(
        "DTCP / CMDA / Local Body", "",
        "Request approved layout / plan / CC / OC from DTCP, CMDA, or the local "
        "planning authority that sanctioned it.",
    ),
    "F": PortalLink(
        "Local Body / Utility", "",
        "Collect the latest tax/utility receipts from the municipality, TWAD/"
        "Metro Water, or TANGEDCO.",
    ),
    "G": PortalLink(
        "Apartment Association / Builder", "",
        "Request from the apartment owners' association or the builder.",
    ),
    "H": PortalLink(
        "Independent Advocate", "",
        "Commission an independent advocate's title search / legal opinion "
        "(the locker will help produce this in a later phase).",
    ),
    "I": PortalLink(
        "Lender / Bank (post-auction)", "",
        "Issued by the bank after you win the auction.",
    ),
}


def portal_link_for(doc_type_id: str) -> PortalLink | None:
    """Best-effort "go get it" guidance for a missing doc type.

    Returns a specific portal link where one is known, otherwise a
    category-level fallback, otherwise ``None`` for unknown ids.
    """
    if doc_type_id in _DOC_TYPE_PORTAL:
        return _DOC_TYPE_PORTAL[doc_type_id]
    cat = DOC_TYPE_TO_CATEGORY.get(doc_type_id)
    return _CATEGORY_PORTAL.get(cat) if cat else None


# ── Helpers for the classifier prompt ────────────────────────────────────────

def render_taxonomy_for_prompt() -> str:
    """Render the taxonomy as a compact ``id — label`` list grouped by category,
    for injection into the classifier prompt so the label set lives in one place.
    """
    lines: list[str] = []
    for cat in CATEGORIES:
        lines.append(f"{cat.id}. {cat.label}")
        for d in DOC_TYPES:
            if d.category == cat.id:
                suffix = "  (conditional)" if d.conditional else ""
                lines.append(f"    {d.id} — {d.label}{suffix}")
    return "\n".join(lines)


def normalize_doc_type(value: str | None) -> str | None:
    """Coerce a classifier/string value to a known doc-type id, the UNKNOWN
    sentinel, or ``None`` (when there is genuinely nothing to record)."""
    if not value:
        return None
    v = value.strip().lower()
    if v in ALL_DOC_TYPE_IDS:
        return v
    if v in ("unknown", "other", "unclassified", ""):
        return UNKNOWN_DOC_TYPE
    return UNKNOWN_DOC_TYPE


def present_doc_type_ids(values: Iterable[str | None]) -> set[str]:
    """Filter an iterable of stored doc-type values down to the set of known,
    checklist-relevant ids (drops None / UNKNOWN)."""
    return {v for v in values if v in ALL_DOC_TYPE_IDS}
