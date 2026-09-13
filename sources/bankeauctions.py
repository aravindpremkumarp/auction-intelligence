"""bankeauctions.com (C1 India) — the platform many private lenders run auctions on.

Legacy server-rendered site: a DataTables JSON endpoint for the list, an
HTML detail page per auction, and a per-auction "View NIT Documents" zip
that carries the sale notice, the newspaper publications and — sometimes —
a property-details PDF with photos. Verified live on 2026-09-12
(docs/source-recon-2026-09.md).

Contract, with its traps:

    POST /home/liveAuctionDatatable/?state=24        filters ride the QUERY STRING,
         body: iDisplayStart=S&iDisplayLength=10&sEcho=1   paging rides the POST BODY
    → {"iTotalRecords": "195", "aaData": [[logo, id, lender, description, city,
         auction date, reserve, EMD, action, --, detail id, 0, category, sub-category, 0], …]}

    - page size is pinned to 10 whatever iDisplayLength says
    - consecutive pages overlap by one row → dedupe on aaData[i][1]
    - detail page: /{category}-{subcategory}-{city}-{col10}, slugified
    - NIT zip under /public/uploads/event_auction/ refuses a bare GET
      ("Invalid: Unauthorize Access") and serves with the detail page as Referer
    - the only <img> on a detail page is the login captcha — never a photo

Tamil Nadu's id here (24) is unrelated to BAANKNET's (31); it is read off the
homepage's own <option> list rather than hard-coded.
"""
from __future__ import annotations

import html as html_lib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sources import http
from sources.base import SOURCE_RANK, DocRef, Listing
from sources.download import download, extract_zip_members
from sources.normalize import clean_price, doc_role_for, make_auction_id, parse_date

SITE = "https://bankeauctions.com"
DATATABLE = f"{SITE}/home/liveAuctionDatatable/"
PAGE_SIZE = 10  # pinned server-side

# Row columns, as the homepage's DataTables config consumes them.
COL_ID, COL_LENDER, COL_DESCRIPTION, COL_CITY, COL_DATE = 1, 2, 3, 4, 5
COL_RESERVE, COL_EMD, COL_ACTION, COL_DETAIL_ID, COL_CATEGORY, COL_SUBCATEGORY = 6, 7, 8, 10, 12, 13

_PAIR = re.compile(r'detl-left"[^>]*>(.*?)</div>\s*<div[^>]*detl-right"[^>]*>(.*?)</div>', re.S)
_TAG = re.compile(r"<[^>]+>")
_STATE_OPTION = re.compile(r'<option\s+value="(\d+)"\s*>\s*([^<]+?)\s*</option>', re.I)
_ZIP = re.compile(r'href="([^"]*?/public/uploads/event_auction/[^"]+\.zip)"', re.I)
# "<label> :</div><div …><a href=…/public/uploads/bank/….pdf">" — the label
# sits in the detl-left cell, the link one or two tags later in detl-right.
_LOOSE_PDF = re.compile(
    r'(?:>|\n)\s*([^<>\n]{3,80}?)\s*:\s*(?:<[^>]*>\s*){1,4}<a[^>]*href="([^"]*?/public/uploads/bank/[^"]+\.pdf)"',
    re.I | re.S,
)

#: What the detail page calls things → Listing fields.
LABELS = {
    "Organisation Name": "org",
    "Event Branch": "branch",
    "Property Category": "category",
    "Property Sub Category": "subcategory",
    "Property Description": "description",
    "Borrower's Name": "borrower",
    "Reserve Price": "reserve",
    "EMD Amount": "emd",
    "Bid Increment value": "increment",
    "Auto Extension time": "extension",
    "EMD Deposit Bank Name": "emd_bank",
    "EMD Deposit Bank Account Number": "emd_account",
    "EMD Deposit Bank IFSC Code": "emd_ifsc",
    "Press Release Date": "press_release",
    "Date of Inspection of Property (From)": "inspection_from",
    "Date of Inspection of Property (To)": "inspection_to",
    "Offer (First Round Quote) Submission Last Date": "offer_deadline",
    "Auction Start Date and Time": "auction_start",
    "Auction End Date and Time": "auction_end",
}


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(_TAG.sub(" ", fragment))).strip()


def slugify(s: str) -> str:
    """The site's own slug rule, as ``checklocation()`` on the homepage does it."""
    return s.replace(" ", "-").replace("(", "").replace(")", "").lower()


def detail_url_for(row: list) -> str:
    return f"{SITE}/{slugify(row[COL_CATEGORY])}-{slugify(row[COL_SUBCATEGORY])}-{slugify(row[COL_CITY])}-{row[COL_DETAIL_ID]}"


def file_slug(s: str) -> str:
    s = re.sub(r"\.pdf$", "", s.strip(), flags=re.I)
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "document"


def parse_detail(html: str) -> dict:
    """The label/value pairs on a detail page, keyed by ``LABELS``."""
    out: dict[str, str] = {}
    for label, value in _PAIR.findall(html):
        key = LABELS.get(_text(label).rstrip(":").strip())
        if key and key not in out:
            out[key] = _text(value)
    return out


def documents_from_detail(html: str, detail_url: str, row_id: str) -> list[DocRef]:
    """The NIT zip (the per-auction bundle) and the loose tender PDFs.

    The site-wide ``Terms___Condition.pdf`` and user agreement sit at the
    root and are neither — only ``/public/uploads/`` links are per-auction.
    """
    docs: list[DocRef] = []
    for href in _ZIP.findall(html):
        url = href if href.startswith("http") else SITE + href
        docs.append(DocRef(url=url, filename=f"be-{row_id}-nit.zip", label="View NIT Documents",
                           doc_role="bundle", needs_referer=True, referer=detail_url))
        break
    seen: set[str] = set()
    for label, href in _LOOSE_PDF.findall(html):
        url = href if href.startswith("http") else SITE + href
        if url in seen:
            continue
        seen.add(url)
        label = _text(label)
        docs.append(DocRef(url=url, filename=f"be-{row_id}-{file_slug(label)}.pdf", label=label,
                           doc_role=doc_role_for(label) if doc_role_for(label) != "unknown" else "tender"))
    return docs


class BankeauctionsAdapter:
    name = "bankeauctions"
    id_prefix = "be-"
    source_rank = SOURCE_RANK["bankeauctions"]

    def __init__(self, *, state: str = "Tamil Nadu", session=None, with_detail: bool = True):
        self.state = state
        self._session = session
        self.with_detail = with_detail
        self._state_id: int | None = None

    @property
    def session(self):
        return self._session or http.get_session()

    # ── network ─────────────────────────────────────────────────────────────
    def state_id(self) -> int:
        """Read the state's <option value> off the homepage; exact name match
        so 'The Tamil Nadu Industrial Investment Corporation' (a lender) cannot
        be mistaken for the state."""
        if self._state_id is None:
            r = self.session.get(SITE + "/")
            r.raise_for_status()
            want = self.state.strip().lower()
            for value, name in _STATE_OPTION.findall(r.text):
                if html_lib.unescape(name).strip().lower() == want:
                    self._state_id = int(value)
                    break
            else:
                raise LookupError(f"state {self.state!r} not in bankeauctions' dropdown")
        return self._state_id

    def _page(self, start: int) -> dict:
        r = self.session.post(
            DATATABLE, params={"state": self.state_id()},
            data={"iDisplayStart": start, "iDisplayLength": PAGE_SIZE, "sEcho": 1},
        )
        r.raise_for_status()
        return r.json()

    def _detail(self, url: str) -> str:
        r = self.session.get(url)
        r.raise_for_status()
        return r.text

    def harvest(self, *, limit: int | None = None) -> Iterator[dict]:
        """Yield ``{"row": aaData row, "detail_url": …, "detail_html": … | None}``."""
        seen: set[str] = set()
        yielded = 0
        start = 0
        total = None
        while True:
            data = self._page(start)
            rows = data.get("aaData") or []
            total = int(data.get("iTotalRecords") or 0) if total is None else total
            new = 0
            for row in rows:
                rid = str(row[COL_ID])
                if rid in seen:
                    continue
                seen.add(rid)
                new += 1
                url = detail_url_for(row)
                detail = self._detail(url) if self.with_detail else None
                yield {"row": row, "detail_url": url, "detail_html": detail}
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            start += PAGE_SIZE
            if not rows or new == 0 or start >= total:
                return

    # ── mapping (pure) ──────────────────────────────────────────────────────
    def normalize(self, raw: dict) -> Listing | None:
        row = raw["row"]
        if str(row[COL_CATEGORY]).strip().lower() != "immovable":
            return None  # SBI lists vehicles and plant here too
        rid = str(row[COL_ID])
        detail_url = raw.get("detail_url") or detail_url_for(row)
        d = parse_detail(raw.get("detail_html") or "")

        reserve_raw = d.get("reserve") or str(row[COL_RESERVE] or "")
        reserve_raw, reserve_num = clean_price(reserve_raw)
        emd_raw = d.get("emd") or str(row[COL_EMD] or "")
        emd_raw, emd_num = clean_price(emd_raw)
        _, increment = clean_price(d.get("increment") or "")

        start_raw = d.get("auction_start") or str(row[COL_DATE] or "")
        end_raw = d.get("auction_end") or ""
        deadline_raw = d.get("offer_deadline") or ""

        description = d.get("description") or str(row[COL_DESCRIPTION] or "")
        subcat = str(row[COL_SUBCATEGORY] or "").strip()
        from sources.baanknet import asset_category_for, property_type_for  # shared lookup helpers

        documents = documents_from_detail(raw.get("detail_html") or "", detail_url, rid)
        action = str(row[COL_ACTION] or "").strip().upper()

        return Listing(
            source=self.name,
            source_id=rid,
            auction_id=make_auction_id(self.id_prefix, rid),
            source_url=detail_url,
            source_rank=self.source_rank,
            url=detail_url,
            title=description[:120],
            description=description,
            reserve_price_raw=reserve_raw,
            reserve_price_num=reserve_num,
            emd_raw=emd_raw,
            emd_num=emd_num,
            auction_start_date_raw=start_raw,
            auction_start_dt=parse_date(start_raw),
            auction_end_time_raw=end_raw,
            auction_end_dt=parse_date(end_raw),
            application_deadline_raw=deadline_raw,
            application_deadline_dt=parse_date(deadline_raw),
            area="",
            city=str(row[COL_CITY] or "").strip().title(),
            state=self.state,
            asset_category=asset_category_for(self.name, subcat),
            property_type_raw=f"{row[COL_CATEGORY]} / {subcat}",
            property_types=[t for t in [property_type_for(self.name, subcat)] if t],
            auction_type="SARFAESI Auction" if action == "SARFAESI" else ("DRT Auction" if action == "DRT" else action),
            bank_name=(d.get("org") or str(row[COL_LENDER] or "")).strip(),
            branch_name=d.get("branch", ""),
            borrower_name=d.get("borrower", ""),
            service_provider="bankeauctions.com",
            contact_details=" ".join(x for x in (d.get("emd_bank"), d.get("emd_account"), d.get("emd_ifsc")) if x),
            downloads_found=[],
            downloads_missing=[doc.filename for doc in documents],
            downloads_complete=not documents,
            district="",
            pincode="",
            borrower_address="",
            possession_type="",
            extent_raw="",
            bid_increment_num=increment,
            inspection_start_dt=parse_date(d.get("inspection_from")),
            inspection_end_dt=parse_date(d.get("inspection_to")),
            auction_status="live",  # the datatable endpoint only lists live auctions
            documents=documents,
            media=[],  # the detail page's one <img> is the login captcha
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    # ── documents ───────────────────────────────────────────────────────────
    def fetch_document(self, ref: DocRef, dest_dir: str | Path) -> Path | None:
        return download(ref.url, dest_dir, filename=ref.filename,
                        referer=ref.referer if ref.needs_referer else None, session=self.session)

    def expand_bundle(self, ref: DocRef, zip_path: str | Path, dest_dir: str | Path) -> list[DocRef]:
        """Unpack a fetched NIT zip into per-document refs, routed by file name."""
        rid = ref.filename.split("-")[1] if ref.filename.startswith("be-") else "x"
        members = extract_zip_members(zip_path, dest_dir, lambda name: f"be-{rid}-{file_slug(Path(name).name)}{Path(name).suffix.lower() or '.pdf'}")
        return [
            DocRef(url=f"{ref.url}#{name}", filename=path.name, label=Path(name).name, doc_role=doc_role_for(Path(name).name))
            for name, path in members
        ]
