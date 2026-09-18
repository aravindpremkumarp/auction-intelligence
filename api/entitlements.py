"""
api/entitlements.py
-------------------
What a visitor sees before they pay, in one place.

The property page used to give everything away. The free view is now the
browse card plus the size the notice states: **property type, where it is,
the bank, the reserve price, the auction date, the extent**. Everything that
takes work to produce — the notice's survey and patta numbers, the
boundaries, the full address and description, the parties and their secured
debt, the authorised officer, the EMD account, the notice PDF, and the list
of what the notice fails to say — is the paid product.

Three rules hold this together:

1. **Allowlist, never denylist.** `/auction/{id}` returns `properties(a)`,
   every property on the node, so a denylist silently leaks each new field
   the loader writes. `FREE_FIELDS` names what may leave the building.

2. **The server redacts, not the browser.** A hidden `<div>` is not a
   paywall. A locked value never reaches the client, so the only way to read
   it is to buy it.

3. **Say what is behind the lock.** Redaction returns counts — "8
   identifiers", "4 boundaries", "2 things this notice does not say" — not
   silence. A locked panel that names what it holds is the reason to
   upgrade; an empty one just looks broken.

Not covered here: the chat agent reads the same graph through
`api/agent3/`, so a free user can still ask it for a survey number. Closing
that is a separate change (see the PR that introduced this module).
"""
from __future__ import annotations

from typing import Any

from api.auth.schemas import UserOut

#: The tier that unlocks everything. `api/auth/dependencies.py::_derive_tier`
#: is what puts a user in it, and it lapses on its own when the plan expires.
PAID_TIER = "paid"


def is_paid(user: UserOut | None) -> bool:
    """True when this caller has bought the unlock. Anonymous is never paid."""
    return bool(user is not None and user.tier == PAID_TIER)


#: AuctionProperty node properties a free visitor may see. Everything else on
#: the node — EMD, deadlines, contact, the scraped description, the notice's
#: boundaries and door numbers, every pipeline scoring field — is withheld.
#: Keep this list short on purpose: a field belongs here only if the browse
#: card already shows it, or it is one of the five the free view promises.
FREE_FIELDS: frozenset[str] = frozenset({
    "auction_id", "title", "url", "source_url",
    # the money and the date — the two facts a card leads with
    "reserve_price_num", "reserve_price_raw", "auction_start_dt",
    # the extent, as the notice states it
    "total_area", "extent_sqft",
    # where it is
    "village", "revenue_village", "district", "revenue_district",
    "taluk", "revenue_taluk", "pincode",
    # what it is
    "property_type_effective", "property_type_norm", "property_type_raw",
    "portal_property_type", "asset_category_norm",
})

#: Related nodes a free visitor may see. `branch` and `borrower` are held
#: back with the rest of the parties: which branch holds the file and who the
#: borrower is are both things the notice tells a buyer, not the portal row.
FREE_RELATIONSHIPS: frozenset[str] = frozenset({
    "city", "area", "state", "bank", "asset_category", "auction_type",
    "property_types",
})

#: Top-level keys of the detail record (outside `fields`) a free visitor may
#: see. `documents` and `other_listings` are withheld — a notice PDF link is
#: the paid artifact, and the copies on other portals are a research result.
FREE_DETAIL_KEYS: frozenset[str] = frozenset({
    "auction_id", "source", "photos", "price_history",
})

#: Named in the locked panel so the visitor knows what they are buying. The
#: order is the order the UI lists them in.
LOCKED_DETAIL_LABELS: tuple[str, ...] = (
    "EMD and bid increment",
    "application deadline and auction close",
    "bank branch and borrower",
    "contact details",
    "the full property description",
    "the sale notice document",
)


def redact_detail(detail: dict[str, Any], paid: bool) -> dict[str, Any]:
    """`/auction/{id}` as this caller may see it.

    A paid caller gets the record untouched. Everyone else gets the free
    fields plus a `locked` block naming what was withheld, so the page can
    render the gate instead of a hole.
    """
    if paid:
        return detail
    fields = detail.get("fields") or {}
    rel = detail.get("relationships") or {}
    out: dict[str, Any] = {
        k: v for k, v in detail.items() if k in FREE_DETAIL_KEYS
    }
    out["fields"] = {k: v for k, v in fields.items() if k in FREE_FIELDS}
    out["relationships"] = {
        k: v for k, v in rel.items() if k in FREE_RELATIONSHIPS
    }
    out["locked"] = {
        "tier_required": PAID_TIER,
        "fields": list(LOCKED_DETAIL_LABELS),
        # Counted, not listed: "the notice links 2 documents" is a reason to
        # pay; the filenames are the thing being sold.
        "document_count": len(detail.get("documents") or []),
        "other_listing_count": len(detail.get("other_listings") or []),
    }
    return out


def _lot_counts(lot: dict[str, Any]) -> dict[str, int]:
    """How much the notice holds for this lot, without saying what it says."""
    return {
        "identifiers": len(lot.get("identifiers") or []),
        "boundaries": len(lot.get("boundaries") or []),
        "parties": len(lot.get("parties") or []),
        "loans": len(lot.get("loans") or []),
        "schedules": len(lot.get("schedules") or []),
        "extents": len(lot.get("extents") or []),
    }


def _identifier_kinds(lot: dict[str, Any]) -> list[str]:
    """Which KINDS of identifier the notice carries — "survey no", "patta no"
    — never their values. Naming the kind is what makes the lock worth
    opening; the value is the product."""
    seen: list[str] = []
    for i in lot.get("identifiers") or []:
        kind = (i or {}).get("kind")
        if kind and kind not in seen:
            seen.append(kind)
    return seen


def redact_notice(bundle: dict[str, Any], paid: bool) -> dict[str, Any]:
    """`/auction/{id}/notice` as this caller may see it.

    The notice entities ARE the paid product, so a free caller gets none of
    the values — only the free facts the page already shows (type, extent,
    possession, village) and a count of what is locked.
    """
    if paid:
        return bundle
    lot = bundle.get("property") or {}
    lots = bundle.get("notice_lots") or []
    # On a multi-lot notice nothing is this property's own, so the free
    # preview stays empty rather than borrowing a sibling lot's extent.
    preview: dict[str, Any] = {}
    counts: dict[str, int] = {}
    kinds: list[str] = []
    if lot:
        preview = {
            k: lot[k] for k in
            ("property_type", "asset_category", "headline_sqft",
             "village", "taluk", "district")
            if lot.get(k) not in (None, "", [])
        }
        if lot.get("possession"):
            preview["possession_type"] = lot["possession"].get("type")
        counts = _lot_counts(lot)
        kinds = _identifier_kinds(lot)
    elif lots:
        counts = {"lots": len(lots)}

    doc = bundle.get("notice") or {}
    return {
        "auction_id": bundle.get("auction_id"),
        "scope": bundle.get("scope"),
        "notice_lot_count": bundle.get("notice_lot_count"),
        "scope_note": bundle.get("scope_note"),
        "property_preview": preview,
        "locked": {
            "tier_required": PAID_TIER,
            "counts": counts,
            "identifier_kinds": kinds,
            "has_emd_account": bool(doc.get("emd_accounts")),
            "has_officer": bool(doc.get("officers")),
            "has_contacts": bool(doc.get("contacts")),
            "has_terms": bool(doc.get("sale_terms")),
            "has_notice_document": bool(doc.get("notice_url")),
            # The gaps are the diligence product, so the count is the teaser
            # and the sentences are behind the lock.
            "gap_count": len(bundle.get("gaps") or []),
        },
    }
