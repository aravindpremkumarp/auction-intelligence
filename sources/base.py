"""The adapter contract and the one record shape every source produces.

``Listing`` is a strict superset of the dict ``scripts/prepare_tn_data.py``
has always written to ``data/tn_auction_data.jsonl`` — same key names, same
value conventions — so the existing loader keeps reading it unchanged, and
the new keys (``source``, ``documents``, ``media`` …) ride alongside.

Everything here is pure data. Network lives in ``harvest()`` /
``fetch_document()`` on the adapter; ``normalize()`` must stay a pure function
of one raw record so each portal's mapping is testable from an inline dict.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable

#: Merge priority when the same auction is carried by several portals. Lower
#: wins. Official disclosure portal > the platform the auction runs on > a
#: re-aggregator of both.
SOURCE_RANK: dict[str, int] = {
    "baanknet": 1,
    "bankeauctions": 2,
    "eauctionsindia": 3,
}

#: What a document *is*, decided by the adapter from the portal's own label or
#: the bundle file name — never by a model. ``sale_notice`` is the one the
#: OCR/extraction path is fed by; ``publication`` is the newspaper branch.
DOC_ROLES = (
    "sale_notice",
    "tender",
    "terms",
    "affidavit",
    "publication",
    "property_details",
    "proclamation",
    "bundle",
    "unknown",
)

MEDIA_KINDS = ("image", "video")


@dataclass(frozen=True)
class DocRef:
    """One document a listing points at.

    ``filename`` must be unique across listings: ``:Document`` is MERGEd on
    the bare filename (``scripts/upload_downloads_to_r2.py``), and BAANKNET
    names every notice ``SALE NOTICE.pdf``. Adapters prefix their id scheme.
    """

    url: str
    filename: str
    label: str | None = None
    doc_role: str = "unknown"
    #: bankeauctions' NIT zip answers "Unauthorize Access" to a bare GET and
    #: serves only to a request carrying the detail page as Referer.
    needs_referer: bool = False
    referer: str | None = None


@dataclass(frozen=True)
class MediaRef:
    url: str
    kind: str = "image"
    is_main: bool = False
    label: str | None = None


@dataclass
class Listing:
    # ── identity ──────────────────────────────────────────────────────────
    source: str
    source_id: str
    auction_id: str
    source_url: str
    source_rank: int
    # ── the prepare_tn_data contract, same names ──────────────────────────
    url: str = ""
    title: str = ""
    description: str = ""
    reserve_price_raw: str | None = ""
    reserve_price_num: float | None = None
    emd_raw: str | None = ""
    emd_num: float | None = None
    auction_start_date_raw: str = ""
    auction_start_dt: str | None = None
    auction_end_time_raw: str = ""
    auction_end_dt: str | None = None
    application_deadline_raw: str = ""
    application_deadline_dt: str | None = None
    area: str = ""
    city: str = ""
    state: str = ""
    asset_category: str = ""
    property_type_raw: str = ""
    property_types: list[str] = field(default_factory=list)
    auction_type: str = ""
    bank_name: str = ""
    branch_name: str = ""
    borrower_name: str = ""
    service_provider: str = ""
    contact_details: str = ""
    downloads_found: list[str] = field(default_factory=list)
    downloads_missing: list[str] = field(default_factory=list)
    downloads_complete: bool = True
    # ── new with the adapters ─────────────────────────────────────────────
    district: str = ""
    pincode: str = ""
    borrower_address: str = ""
    possession_type: str = ""
    extent_raw: str = ""
    bid_increment_num: float | None = None
    inspection_start_dt: str | None = None
    inspection_end_dt: str | None = None
    auction_status: str = ""
    documents: list[DocRef] = field(default_factory=list)
    media: list[MediaRef] = field(default_factory=list)
    fetched_at: str = ""

    @property
    def downloads_list(self) -> list[str]:
        return [d.filename for d in self.documents]

    @property
    def has_photos(self) -> bool:
        return any(m.kind == "image" for m in self.media)

    def to_row(self) -> dict:
        """The JSON row for ``data/listings/<source>.jsonl``.

        Legacy keys come first, in the order ``prepare_tn_data`` wrote them,
        so a diff against the old file reads as pure additions.
        """
        return {
            "auction_id": self.auction_id,
            "url": self.url,
            "title": self.title,
            "description": self.description,
            "reserve_price_raw": self.reserve_price_raw,
            "reserve_price_num": self.reserve_price_num,
            "emd_raw": self.emd_raw,
            "emd_num": self.emd_num,
            "auction_start_date_raw": self.auction_start_date_raw,
            "auction_start_dt": self.auction_start_dt,
            "auction_end_time_raw": self.auction_end_time_raw,
            "auction_end_dt": self.auction_end_dt,
            "application_deadline_raw": self.application_deadline_raw,
            "application_deadline_dt": self.application_deadline_dt,
            "area": self.area,
            "city": self.city,
            "state": self.state,
            "asset_category": self.asset_category,
            "property_type_raw": self.property_type_raw,
            "property_types": list(self.property_types),
            "auction_type": self.auction_type,
            "bank_name": self.bank_name,
            "branch_name": self.branch_name,
            "borrower_name": self.borrower_name,
            "service_provider": self.service_provider,
            "contact_details": self.contact_details,
            "downloads_list": self.downloads_list,
            "downloads_found": list(self.downloads_found),
            "downloads_missing": list(self.downloads_missing),
            "downloads_complete": self.downloads_complete,
            # ── additions ──
            "source": self.source,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "source_rank": self.source_rank,
            "district": self.district,
            "pincode": self.pincode,
            "borrower_address": self.borrower_address,
            "possession_type": self.possession_type,
            "extent_raw": self.extent_raw,
            "bid_increment_num": self.bid_increment_num,
            "inspection_start_dt": self.inspection_start_dt,
            "inspection_end_dt": self.inspection_end_dt,
            "auction_status": self.auction_status,
            "documents": [asdict(d) for d in self.documents],
            "media": [asdict(m) for m in self.media],
            "has_photos": self.has_photos,
            "fetched_at": self.fetched_at,
        }


#: The 30 keys ``prepare_tn_data`` has always written, in order. The
#: regression test holds the eauctionsindia adapter to these.
LEGACY_ROW_KEYS = (
    "auction_id", "url", "title", "description",
    "reserve_price_raw", "reserve_price_num", "emd_raw", "emd_num",
    "auction_start_date_raw", "auction_start_dt",
    "auction_end_time_raw", "auction_end_dt",
    "application_deadline_raw", "application_deadline_dt",
    "area", "city", "state",
    "asset_category", "property_type_raw", "property_types", "auction_type",
    "bank_name", "branch_name", "borrower_name", "service_provider", "contact_details",
    "downloads_list", "downloads_found", "downloads_missing", "downloads_complete",
)


@runtime_checkable
class SourceAdapter(Protocol):
    """What every portal adapter provides.

    ``harvest`` is the only place a listing-level network call happens;
    ``fetch_document`` is the only place a document-level one does.
    ``normalize`` is pure.
    """

    name: str
    id_prefix: str
    source_rank: int
    state: str

    def harvest(self, *, limit: int | None = None) -> Iterator[dict]:
        """Yield raw records exactly as the portal gave them."""
        ...

    def normalize(self, raw: dict) -> Listing | None:
        """Map one raw record to a Listing, or ``None`` to drop it."""
        ...

    def fetch_document(self, ref: DocRef, dest_dir: Path) -> Path | None:
        """Put the bytes behind ``ref`` at ``dest_dir / ref.filename``."""
        ...
