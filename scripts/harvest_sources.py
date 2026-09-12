"""
harvest_sources.py — pull listings from every portal adapter into one shape.

For each source:
  raw records        → data/raw/<source>/<YYYY-MM-DD>.jsonl   (append; verbatim, never edited)
  normalized rows    → data/listings/<source>.jsonl           (rewritten each run)
  documents          → downloads/<source>/                    (notices, bundles unpacked)
  photos (live only) → downloads/<source>/media/

The raw layer is what makes a mapping bug a re-run instead of a re-scrape.
Nothing here touches Neo4j or R2 — that is scripts/load_tn_to_neo4j.py and
scripts/upload_downloads_to_r2.py, which read data/listings/*.jsonl.

Usage:
    python -m scripts.harvest_sources                       # all sources, Tamil Nadu
    python -m scripts.harvest_sources --source baanknet --limit 20
    python -m scripts.harvest_sources --source bankeauctions --no-download
    python -m scripts.harvest_sources --live-only           # BAANKNET: skip the closed-auction archive

Exit status is non-zero when a source yields no listings — a portal that
answers with nothing is a finding, not a quiet success.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sources import ADAPTERS, get_adapter  # noqa: E402
from sources.base import DocRef, Listing, MediaRef  # noqa: E402
from sources.download import download, filename_from_url  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DOWNLOADS_DIR = PROJECT_ROOT / "downloads"


@dataclasses.dataclass
class Summary:
    source: str
    harvested: int = 0
    listings: int = 0
    dropped: int = 0
    documents_fetched: int = 0
    documents_failed: int = 0
    bundle_members: int = 0
    media_fetched: int = 0
    media_failed: int = 0
    seconds: float = 0.0

    def line(self) -> str:
        return (f"{self.source:<15} harvested={self.harvested:<5} listings={self.listings:<5} dropped={self.dropped:<5} "
                f"docs={self.documents_fetched}/{self.documents_fetched + self.documents_failed:<5} "
                f"members={self.bundle_members:<4} photos={self.media_fetched}/{self.media_fetched + self.media_failed:<5} "
                f"{self.seconds:.0f}s")


def resolve_documents(adapter, listing: Listing, dest_dir: Path, summary: Summary) -> None:
    """Fetch every document the listing points at; unpack bundles into their
    members, which replace the bundle in ``listing.documents`` (the zip itself
    is not something OCR can read). Updates the found/missing lists in place.
    """
    resolved: list[DocRef] = []
    for ref in listing.documents:
        path = adapter.fetch_document(ref, dest_dir)
        if path is None:
            summary.documents_failed += 1
            resolved.append(ref)
            continue
        summary.documents_fetched += 1
        if ref.doc_role == "bundle" and hasattr(adapter, "expand_bundle"):
            members = adapter.expand_bundle(ref, path, dest_dir)
            summary.bundle_members += len(members)
            resolved.extend(members)
        else:
            resolved.append(ref)
    listing.documents = resolved
    names = [d.filename for d in resolved]
    listing.downloads_found = [n for n in names if (dest_dir / n).exists()]
    listing.downloads_missing = [n for n in names if not (dest_dir / n).exists()]
    listing.downloads_complete = not listing.downloads_missing


def fetch_media(adapter, listing: Listing, media_dir: Path, summary: Summary) -> None:
    """Mirror photos of live listings. Videos stay links (2 MB+ each)."""
    if listing.auction_status != "live":
        return
    for m in listing.media:
        if m.kind != "image":
            continue
        name = f"{adapter.id_prefix}{filename_from_url(m.url)}"
        path = download(m.url, media_dir, filename=name, session=getattr(adapter, "session", None))
        if path is None:
            summary.media_failed += 1
        else:
            summary.media_fetched += 1


def run(
    *,
    sources: list[str],
    state: str = "Tamil Nadu",
    limit: int | None = None,
    do_download: bool = True,
    do_media: bool = True,
    live_only: bool = False,
    data_dir: Path = DATA_DIR,
    downloads_dir: Path = DOWNLOADS_DIR,
    adapters: dict | None = None,
) -> list[Summary]:
    """Harvest each source; returns one Summary per source. ``adapters`` lets
    a test hand in ready-made instances instead of the registry."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    summaries: list[Summary] = []
    for name in sources:
        started = time.monotonic()
        summary = Summary(source=name)
        if adapters and name in adapters:
            adapter = adapters[name]
        else:
            kwargs = {"state": state}
            if name == "baanknet":
                kwargs["live_only"] = live_only
            adapter = get_adapter(name, **kwargs)

        raw_dir = data_dir / "raw" / name
        raw_dir.mkdir(parents=True, exist_ok=True)
        listings_dir = data_dir / "listings"
        listings_dir.mkdir(parents=True, exist_ok=True)
        dest_dir = downloads_dir / name
        media_dir = dest_dir / "media"

        rows: list[dict] = []
        with open(raw_dir / f"{today}.jsonl", "a", encoding="utf-8") as raw_out:
            for raw in adapter.harvest(limit=limit):
                summary.harvested += 1
                raw_out.write(json.dumps({"source": name, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "raw": raw},
                                         ensure_ascii=False, default=str) + "\n")
                listing = adapter.normalize(raw)
                if listing is None:
                    summary.dropped += 1
                    continue
                if do_download:
                    resolve_documents(adapter, listing, dest_dir, summary)
                    if do_media:
                        fetch_media(adapter, listing, media_dir, summary)
                rows.append(listing.to_row())
                summary.listings += 1

        with open(listings_dir / f"{name}.jsonl", "w", encoding="utf-8") as out:
            for row in rows:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")

        summary.seconds = time.monotonic() - started
        summaries.append(summary)
        print(summary.line(), flush=True)
    return summaries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="append", default=None,
                    help=f"one of {sorted(ADAPTERS)} or 'all' (repeatable; default all)")
    ap.add_argument("--state", default="Tamil Nadu")
    ap.add_argument("--limit", type=int, default=None, help="stop after N raw records per source")
    ap.add_argument("--no-download", action="store_true", help="do not fetch documents")
    ap.add_argument("--no-media", action="store_true", help="do not mirror photos")
    ap.add_argument("--live-only", action="store_true", help="BAANKNET: skip properties without an open auction")
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    ap.add_argument("--downloads-dir", default=str(DOWNLOADS_DIR))
    args = ap.parse_args(argv)

    wanted = args.source or ["all"]
    if "all" in wanted:
        wanted = list(ADAPTERS)
    unknown = [s for s in wanted if s not in ADAPTERS]
    if unknown:
        ap.error(f"unknown source(s) {unknown}; known: {sorted(ADAPTERS)}")

    summaries = run(
        sources=wanted, state=args.state, limit=args.limit,
        do_download=not args.no_download, do_media=not args.no_media, live_only=args.live_only,
        data_dir=Path(args.data_dir), downloads_dir=Path(args.downloads_dir),
    )
    print("=" * 72)
    for s in summaries:
        print(s.line())
    empty = [s.source for s in summaries if s.listings == 0]
    if empty:
        print(f"no listings from: {', '.join(empty)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    # Line-buffered UTF-8 so the per-source line shows as each finishes.
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
