"""BAANKNET (baanknet.com) — the PSB Alliance disclosure portal.

A Next.js front end over a public, unauthenticated JSON API. One search row
already carries 85 fields; the auction record adds the sale-notice PDF and
the photos. Everything below was verified live on 2026-09-12
(docs/source-recon-2026-09.md).

Contract:

    GET  /api/v1/common/states?countryId=101         → Tamil Nadu is id 31 (its "code" is 33 — that is Tripura's id)
    POST /api/v1/property/detail/property-filter     {"search": {"stateId": 31}, "sort": {"type": "mostrecent"},
                                                       "range": "", "page": p, "limit": 50}
    GET  /api/v1/auction/detail/{auctionId}          reserve, EMD, increment, inspection, auctionDocuments[], propertyMedia[]

Traps the adapter absorbs: ``search`` must be an object (a top-level
``state`` is silently ignored and you get the national list); the two
detail endpoints are keyed on *different* ids that share a numeric range;
every notice is named ``SALE NOTICE.pdf``, so document filenames are built
from the CDN basename with a ``bn-`` prefix.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sources import http
from sources.base import SOURCE_RANK, DocRef, Listing, MediaRef
from sources.download import download, filename_from_url
from sources.normalize import clean_price, doc_role_for, make_auction_id, parse_date

API = "https://baanknet.com/api/v1"
SITE = "https://baanknet.com"
INDIA_COUNTRY_ID = 101
PAGE_SIZE = 50

_LOOKUP_PATH = Path(__file__).with_name("lookups") / "asset_categories.json"
_lookup_cache: dict | None = None

DOCUMENT_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png")
#: BAANKNET's PropertyPhotoFileType enum: 1 image, 2 video.
MEDIA_KIND = {1: "image", 2: "video", "1": "image", "2": "video"}


def _lookup() -> dict:
    global _lookup_cache
    if _lookup_cache is None:
        with open(_LOOKUP_PATH, encoding="utf-8") as f:
            _lookup_cache = json.load(f)
    return _lookup_cache


def asset_category_for(source: str, raw: str | None) -> str:
    """Portal category → the graph's ``:AssetCategory`` name, or ``""``.

    An unmapped value is returned as-is so nothing is silently dropped; an
    explicitly unknowable one (BAANKNET's "Other") maps to ``""``.
    """
    if not raw:
        return ""
    table = _lookup().get(source, {}).get("asset_category", {})
    return table.get(raw, table.get(raw.strip().title(), raw.strip()))


def property_type_for(source: str, raw: str | None) -> str:
    """Portal sub-type → the graph's ``:PropertyType`` name (raw if unmapped)."""
    if not raw:
        return ""
    table = _lookup().get(source, {}).get("property_type", {})
    return table.get(raw, table.get(raw.strip().title(), raw.strip()))


def auction_type_for(raw: str | None) -> str:
    """``"Under SARFAESI"`` → ``"SARFAESI Auction"``, etc."""
    if not raw:
        return ""
    low = raw.lower()
    if "sarfaesi" in low:
        return "SARFAESI Auction"
    if "drt" in low:
        return "DRT Auction"
    if "ibc" in low or "liquidat" in low:
        return "Liquidation Auction"
    return raw.strip()


def _is_main_flag(value) -> bool:
    """``ismainimage`` arrives as ``"base64:type16:AQ=="`` (\\x01) or
    ``"base64:type16:AA=="`` (\\x00) — a MySQL BIT leaking through the API."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().endswith("AQ==") or value.strip().lower() in ("1", "true")
    return False


class BaanknetAdapter:
    name = "baanknet"
    id_prefix = "bn-"
    source_rank = SOURCE_RANK["baanknet"]

    def __init__(
        self,
        *,
        state: str = "Tamil Nadu",
        session=None,
        page_size: int = PAGE_SIZE,
        with_detail: bool = True,
        live_only: bool = False,
    ):
        self.state = state
        self._session = session
        self.page_size = page_size
        #: The auction record is one more request per row; it is where the
        #: notice PDF and the photos are, so it is on by default.
        self.with_detail = with_detail
        #: The archive (closed auctions) is what re-auction history is built
        #: from, so the default sweep keeps it.
        self.live_only = live_only
        self._state_id: int | None = None

    # ── network ─────────────────────────────────────────────────────────────
    @property
    def session(self):
        return self._session or http.get_session()

    def state_id(self) -> int:
        """Resolve the state name through the portal's own list — never hard-code."""
        if self._state_id is None:
            r = self.session.get(f"{API}/common/states", params={"countryId": INDIA_COUNTRY_ID})
            r.raise_for_status()
            rows = r.json().get("data") or []
            want = self.state.strip().lower()
            for row in rows:
                if str(row.get("name", "")).strip().lower() == want:
                    self._state_id = int(row["id"])
                    break
            else:
                raise LookupError(f"state {self.state!r} not in BAANKNET's list: {[r.get('name') for r in rows]}")
        return self._state_id

    def _search_page(self, page: int) -> dict:
        body = {
            "search": {"stateId": self.state_id()},
            "sort": {"type": "mostrecent"},
            "range": "",
            "page": page,
            "limit": self.page_size,
        }
        r = self.session.post(f"{API}/property/detail/property-filter", json=body)
        r.raise_for_status()
        return r.json()["data"]

    def _auction_detail(self, auction_id) -> dict | None:
        r = self.session.get(f"{API}/auction/detail/{auction_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return (r.json() or {}).get("data")

    def harvest(self, *, limit: int | None = None) -> Iterator[dict]:
        """Yield ``{"row": <search _source>, "detail": <auction record or None>}``."""
        yielded = 0
        page = 1
        seen: set[str] = set()
        while True:
            data = self._search_page(page)
            hits = data.get("data") or []
            if not hits:
                return
            for hit in hits:
                row = hit.get("_source") or {}
                key = str(hit.get("_id") or row.get("propertyDetailId"))
                if key in seen:
                    continue
                seen.add(key)
                if self.live_only and not row.get("isAuctionAvailable"):
                    continue
                detail = None
                if self.with_detail and row.get("auctionId"):
                    detail = self._auction_detail(row["auctionId"])
                yield {"row": row, "detail": detail}
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            if page >= int(data.get("totalPages") or 0):
                return
            page += 1

    # ── mapping (pure) ──────────────────────────────────────────────────────
    def normalize(self, raw: dict) -> Listing | None:
        row = raw.get("row", raw) if isinstance(raw, dict) else {}
        detail = raw.get("detail") or {} if isinstance(raw, dict) else {}

        auction_id = row.get("auctionId") or detail.get("auctionId")
        if not auction_id:
            return None  # a property with no auction is not a listing
        if self.state and str(row.get("stateName") or detail.get("stateName") or "").strip().lower() != self.state.lower():
            return None

        pdid = row.get("propertyDetailId") or detail.get("propertyDetailId")
        reserve_raw = detail.get("reservePrice") if detail.get("reservePrice") is not None else row.get("auctionPrice")
        reserve_raw, reserve_num = clean_price("" if reserve_raw is None else str(reserve_raw))
        emd_raw, emd_num = clean_price("" if detail.get("emd") is None else str(detail.get("emd")))

        start_raw = row.get("auctionStartTime") or detail.get("auctionFrom") or ""
        end_raw = row.get("auctionEndTime") or detail.get("auctionTo") or ""
        emd_end_raw = row.get("emdEndTime") or detail.get("emdEnd") or ""

        prop_type = row.get("propertyType") or detail.get("propertyType") or ""
        sub_type = row.get("propertySubType") or detail.get("propertySubType") or ""
        action = detail.get("typeOfAction") or row.get("propTypeOfAction") or ""

        title = row.get("propertyTitle") or detail.get("address") or row.get("propertyHeading") or ""
        address = detail.get("address") or ""
        description = title if not address or address == title else f"{title}\n{address}"

        documents: list[DocRef] = []
        for doc in detail.get("auctionDocuments") or []:
            url = doc.get("url") or ""
            if not url.lower().endswith(DOCUMENT_EXTENSIONS):
                continue
            label = doc.get("description") or doc.get("filename")
            documents.append(DocRef(
                url=url,
                filename=f"{self.id_prefix}{filename_from_url(url)}",
                label=label,
                doc_role=doc_role_for(label),
            ))

        media: list[MediaRef] = []
        for m in detail.get("propertyMedia") or []:
            url = m.get("url") or ""
            if not url:
                continue
            ext = os.path.splitext(url.split("?")[0])[1].lower()
            kind = MEDIA_KIND.get(m.get("filetype"), "video" if ext in (".mp4", ".mov", ".webm") else "image")
            media.append(MediaRef(url=url, kind=kind, is_main=_is_main_flag(m.get("ismainimage")), label=m.get("filename")))
        if not media:
            for i, url in enumerate(row.get("photos") or []):
                media.append(MediaRef(url=url, kind="image", is_main=(i == 0), label=None))
        if media and not any(m.is_main for m in media):
            media[0] = MediaRef(url=media[0].url, kind=media[0].kind, is_main=True, label=media[0].label)

        extent_bits = []
        unit = detail.get("measurementAbb") or row.get("measurementAbb") or ""
        for key, label in (("carpetAreaSqft", "carpet"), ("builtupAreaSqft", "built-up")):
            v = detail.get(key)
            if v not in (None, "", 0, "0", "0.00"):
                extent_bits.append(f"{label} {v} {unit}".strip())
        for key, label in (("area", "area"), ("builtUpArea", "built-up")):
            v = row.get(key)
            if v not in (None, "", 0, "0") and not extent_bits:
                extent_bits.append(f"{label} {v} {unit}".strip())

        status = row.get("auctionStatus", detail.get("auctionStatus"))
        end_dt = parse_date(end_raw)
        if status == 1 and end_dt and end_dt >= datetime.now().strftime("%Y-%m-%dT%H:%M:%S"):
            auction_status = "live"
        elif status == 1:
            auction_status = "ended"
        else:
            auction_status = str(status) if status is not None else ""

        contact = " ".join(x for x in (row.get("inspectionName") or detail.get("inspectionName"),
                                       row.get("inspectionMobileNo") or detail.get("inspectionMobileNo")) if x)

        return Listing(
            source=self.name,
            source_id=str(auction_id),
            auction_id=make_auction_id(self.id_prefix, auction_id),
            source_url=f"{SITE}/property-detail/{pdid}" if pdid else SITE,
            source_rank=self.source_rank,
            url=f"{SITE}/property-detail/{pdid}" if pdid else SITE,
            title=title,
            description=description,
            reserve_price_raw=reserve_raw,
            reserve_price_num=reserve_num,
            emd_raw=emd_raw,
            emd_num=emd_num,
            auction_start_date_raw=start_raw,
            auction_start_dt=parse_date(start_raw),
            auction_end_time_raw=end_raw,
            auction_end_dt=end_dt,
            application_deadline_raw=emd_end_raw,
            application_deadline_dt=parse_date(emd_end_raw),
            area=row.get("locality") or detail.get("locality") or "",
            city=row.get("cityName") or detail.get("cityName") or "",
            state=row.get("stateName") or detail.get("stateName") or "",
            asset_category=asset_category_for(self.name, prop_type),
            property_type_raw=" / ".join(x for x in (prop_type, sub_type) if x),
            property_types=[t for t in [property_type_for(self.name, sub_type)] if t],
            auction_type=auction_type_for(action),
            bank_name=row.get("bankName") or detail.get("propertyBankName") or "",
            branch_name=row.get("departmentName") or detail.get("propertyBranchName") or detail.get("auctionBranch") or "",
            borrower_name=row.get("borrowerName") or detail.get("borrowerName") or "",
            service_provider="BAANKNET",
            contact_details=contact,
            downloads_found=[],
            downloads_missing=[d.filename for d in documents],
            downloads_complete=not documents,
            district=row.get("districtName") or detail.get("districtName") or "",
            pincode=str(row.get("pincode") or detail.get("pincode") or ""),
            borrower_address=row.get("borrowerAddress") or detail.get("borrowerAddress") or "",
            possession_type=str(row.get("propertyPossessionType") or detail.get("propertyPossessionType") or "").strip().lower(),
            extent_raw="; ".join(extent_bits),
            bid_increment_num=float(detail["incrementPrice"]) if detail.get("incrementPrice") not in (None, "") else None,
            inspection_start_dt=parse_date(row.get("inspectionStart") or detail.get("inspectionStart")),
            inspection_end_dt=parse_date(row.get("inspectionEnd") or detail.get("inspectionEnd")),
            auction_status=auction_status,
            documents=documents,
            media=media,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def fetch_document(self, ref: DocRef, dest_dir: str | Path) -> Path | None:
        return download(ref.url, dest_dir, filename=ref.filename, session=self.session)
