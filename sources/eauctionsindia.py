"""eauctionsindia.com — a thin adapter over the existing scrape output.

The Selenium scrapers (``scrapers/phase1_harvest_urls.py`` and
``phase2_scrape_details.py``) are untouched; they still write
``data/live_eauction_data.jsonl`` and put the files under
``downloads/live_properties/``. This adapter reads that file and reproduces
the mapping ``scripts/prepare_tn_data.py`` has always done — key for key,
including both spellings of the keys that drifted — and adds the source
fields every adapter carries.

Ids stay bare (no prefix): the six-digit URL tail is what URLs, watchlists
and ``:InvestmentTracker`` already hold.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sources.base import SOURCE_RANK, DocRef, Listing
from sources.normalize import clean_price, make_auction_id, parse_date

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "live_eauction_data.jsonl"
DEFAULT_DOWNLOAD_DIR = PROJECT_ROOT / "downloads" / "live_properties"
BASE_URL = "https://www.eauctionsindia.com"

#: Kept in sync with scripts/remove_non_property_categories.py, which already
#: purged these from the live graph; without the filter a fresh scrape would
#: quietly put them back.
UNWANTED_CATEGORIES = {"Vehicle Auctions", "Scrap, Plant & Machinery", "Others", "Gold Auctions"}


def split_downloads(raw) -> list[str]:
    """``'a.pdf; b.jpg'`` → ``['a.pdf', 'b.jpg']``; drops blanks and ``N/A``."""
    if not raw or str(raw).strip().lower() in ("none", "n/a", ""):
        return []
    parts = re.split(r"[;,]", str(raw))
    return [p.strip() for p in parts if p.strip() and p.strip().upper() != "N/A"]


def validate_downloads(file_list: list[str], download_dir: str | Path) -> tuple[list[str], list[str]]:
    """Partition filenames into (on disk, missing) under ``download_dir``."""
    found, missing = [], []
    for fname in file_list:
        (found if os.path.exists(os.path.join(download_dir, fname)) else missing).append(fname)
    return found, missing


class EauctionsIndiaAdapter:
    name = "eauctionsindia"
    id_prefix = ""
    source_rank = SOURCE_RANK["eauctionsindia"]

    def __init__(
        self,
        *,
        state: str = "Tamil Nadu",
        input_path: str | Path = DEFAULT_INPUT,
        download_dir: str | Path = DEFAULT_DOWNLOAD_DIR,
    ):
        self.state = state
        self.input_path = Path(input_path)
        self.download_dir = Path(download_dir)

    # ── contract ────────────────────────────────────────────────────────────
    def harvest(self, *, limit: int | None = None) -> Iterator[dict]:
        """Yield the scrape's raw records in file order. Malformed lines are
        skipped, as ``prepare_tn_data`` always skipped them."""
        yielded = 0
        with open(self.input_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield raw
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

    def normalize(self, r: dict) -> Listing | None:
        """One scraped record → Listing, or ``None`` when it is not a Tamil
        Nadu property listing. Mirrors ``prepare_tn_data.py`` line for line."""
        state = str(r.get("Province/State", "")).strip()
        if self.state.lower() not in state.lower():
            return None
        if str(r.get("Asset Category", "")).strip() in UNWANTED_CATEGORIES:
            return None

        # The portal started rendering 'ReservePrice' (no space) in June 2026;
        # reading only the spaced spelling loaded 214 listings with no price.
        rp_raw, rp_num = clean_price(r.get("Reserve Price") or r.get("ReservePrice", ""))
        emd_raw, emd_num = clean_price(r.get("EMD", ""))

        # Old typo key and the corrected one.
        app_date_raw = r.get("Application Submission Date") or r.get("Application Subbmision Date", "")
        start_raw = r.get("Auction Start Date", "")
        end_raw = r.get("Auction End Time", "")

        dl_list = split_downloads(r.get("Downloads", ""))
        found, missing = validate_downloads(dl_list, self.download_dir)

        url = r.get("URL", "") or ""
        native_id = r.get("auction_id") or url.rstrip("/").split("/")[-1]
        property_type_raw = r.get("Property Type", "") or ""

        return Listing(
            source=self.name,
            source_id=str(native_id),
            auction_id=make_auction_id(self.id_prefix, native_id),
            source_url=url,
            source_rank=self.source_rank,
            url=url,
            title=r.get("Title", "") or "",
            description=(r.get("Description", "") or "").split("Province/State :")[0].strip(),
            reserve_price_raw=rp_raw,
            reserve_price_num=rp_num,
            emd_raw=emd_raw,
            emd_num=emd_num,
            auction_start_date_raw=start_raw,
            auction_start_dt=parse_date(start_raw),
            auction_end_time_raw=end_raw,
            auction_end_dt=parse_date(end_raw),
            application_deadline_raw=app_date_raw,
            application_deadline_dt=parse_date(app_date_raw),
            area=r.get("Area/Town", "") or "",
            city=r.get("City/Town", "") or "",
            state=state,
            asset_category=r.get("Asset Category", "") or "",
            property_type_raw=property_type_raw,
            property_types=[p.strip() for p in property_type_raw.split(",") if p.strip()],
            # 'Auction Type' on older runs, 'AuctionType' on newer ones.
            auction_type=r.get("Auction Type") or r.get("AuctionType", "") or "",
            bank_name=r.get("Bank Name", "") or "",
            branch_name=r.get("Branch Name", "") or "",
            borrower_name=r.get("Borrower Name", "") or "",
            service_provider=r.get("Service Provider", "") or "",
            contact_details=r.get("Contact Details", "") or "",
            downloads_found=found,
            downloads_missing=missing,
            downloads_complete=len(missing) == 0,
            # The scrape kept only local filenames, not the portal URLs; the
            # bytes are already on disk, so the ref points at the file.
            documents=[
                DocRef(url=(self.download_dir / f).as_uri() if self.download_dir.is_absolute() else "",
                       filename=f, label=None, doc_role="unknown")
                for f in dl_list
            ],
            media=[],
            fetched_at=str(r.get("_scraped_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )

    def fetch_document(self, ref: DocRef, dest_dir: str | Path) -> Path | None:
        """phase2 already downloaded everything; report what is on disk."""
        candidate = self.download_dir / ref.filename
        return candidate if candidate.exists() else None

    # ── convenience ─────────────────────────────────────────────────────────
    def iter_listings(self, *, limit: int | None = None) -> Iterator[Listing]:
        for raw in self.harvest():
            listing = self.normalize(raw)
            if listing is None:
                continue
            yield listing
            if limit is not None:
                limit -= 1
                if limit <= 0:
                    return
