"""
scripts/upload_downloads_to_r2.py
---------------------------------
Backfill and forward-fill auction downloads and listing photos to Cloudflare R2.

For every AuctionProperty that has a non-empty ``downloads_list``, this
script:

1. Locates each listed file under ``downloads/<source>/`` (the harvest's
   layout) or one of the legacy scraper directories.
2. Uploads it to R2 at ``notices/{auction_id}/{filename}`` (idempotent —
   HEAD check skips already-uploaded objects).
3. Upserts a ``:Document`` node — one canonical node per filename across
   auctions — with ``storage_key``, ``public_url``, ``content_type``,
   ``doc_type``, ``uploaded_at``, and the adapter's ``source`` and
   ``doc_role`` (sale_notice / tender / publication / …, never a model's
   guess), and links it to the property via ``[:HAS_DOCUMENT]``.

Then, for every photo (``:Media {kind: 'image'}``) of a live listing that is
not yet mirrored, it uploads the harvested copy to
``media/{auction_id}/{sha256}.{ext}`` and sets ``content_sha256``, ``r2_key``,
``public_url``. Videos stay CDN links.

Run standalone:
    python -m scripts.upload_downloads_to_r2 --dry-run
    python -m scripts.upload_downloads_to_r2
    python -m scripts.upload_downloads_to_r2 --auction-id bn-359826
    python -m scripts.upload_downloads_to_r2 --no-photos
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

# Support running as a module (python -m scripts.upload_downloads_to_r2) and
# as a plain script from anywhere.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import storage
from pipeline.config import DOWNLOADS_DIR
from api.neo4j_client import run_query
from sources.base import ID_PREFIX
from sources.download import filename_from_url

# The scraper has written files to a few layouts over time. Search each
# known location so the script works against any historical dataset; the
# harvest's ``downloads/<source>/`` is tried first when the source is known.
_DOWNLOAD_SEARCH_DIRS = [
    DOWNLOADS_DIR / "live_properties",
    DOWNLOADS_DIR / "tn_properties",
    DOWNLOADS_DIR,
]

FETCH_CYPHER = """
MATCH (a:AuctionProperty)
WHERE a.downloads_list IS NOT NULL AND size(a.downloads_list) > 0
  AND ($auction_id IS NULL OR a.auction_id = $auction_id)
RETURN a.auction_id AS auction_id, a.downloads_list AS downloads_list,
       a.source AS source, a.document_roles AS document_roles
"""

# Photos of live listings the harvest mirrored but R2 does not hold yet.
FETCH_MEDIA_CYPHER = """
MATCH (a:AuctionProperty)-[:HAS_MEDIA]->(m:Media)
WHERE m.kind = 'image' AND m.r2_key IS NULL AND a.auction_status = 'live'
  AND ($auction_id IS NULL OR a.auction_id = $auction_id)
RETURN a.auction_id AS auction_id, a.source AS source, m.url AS url
"""

UPDATE_MEDIA_CYPHER = """
MATCH (m:Media {url: $url})
SET m.content_sha256 = $sha, m.r2_key = $key, m.public_url = $public_url,
    m.content_type = $content_type, m.uploaded_at = datetime()
"""

# One canonical :Document per filename across all auctions. MERGE on the
# node first so the same filename collapses across multiple auctions, then
# MERGE the relationship separately. coalesce on storage_key / public_url
# so we never overwrite a canonical's R2 location with a fresh upload that
# was supposed to be a dedup.
UPSERT_DOC_CYPHER = """
MATCH (a:AuctionProperty {auction_id: $auction_id})
MERGE (doc:Document {filename: $filename})
ON CREATE SET doc.storage_key  = $storage_key,
              doc.public_url   = $public_url,
              doc.content_type = $content_type,
              doc.doc_type     = $doc_type,
              doc.file_path    = $storage_key,
              doc.source       = $source,
              doc.doc_role     = $doc_role,
              doc.uploaded_at  = datetime()
ON MATCH  SET doc.storage_key  = coalesce(doc.storage_key,  $storage_key),
              doc.public_url   = coalesce(doc.public_url,   $public_url),
              doc.content_type = coalesce(doc.content_type, $content_type),
              doc.doc_type     = coalesce(doc.doc_type,     $doc_type),
              doc.file_path    = coalesce(doc.file_path,    $storage_key),
              doc.source       = coalesce(doc.source,       $source),
              doc.doc_role     = coalesce(doc.doc_role,     $doc_role)
MERGE (a)-[:HAS_DOCUMENT]->(doc)
"""

# Look up an existing canonical Document so repeat uploads of the same
# filename across multiple auctions reuse the same R2 object instead of
# creating a per-auction copy.
LOOKUP_CANONICAL_CYPHER = """
MATCH (doc:Document {filename: $filename})
WHERE doc.storage_key IS NOT NULL
RETURN doc.storage_key  AS storage_key,
       doc.public_url   AS public_url,
       doc.content_type AS content_type
LIMIT 1
"""


@dataclass
class UploadResult:
    uploaded: int = 0
    skipped_exists: int = 0
    skipped_missing: int = 0
    reused_canonical: int = 0
    graph_updates: int = 0
    errors: int = 0
    photos_uploaded: int = 0
    photos_exist: int = 0
    photos_missing: int = 0


def lookup_canonical(filename: str) -> dict | None:
    rows = run_query(LOOKUP_CANONICAL_CYPHER, {"filename": filename})
    return rows[0] if rows else None


def locate_local_file(filename: str, source: str | None = None) -> Path | None:
    dirs = ([DOWNLOADS_DIR / source] if source else []) + _DOWNLOAD_SEARCH_DIRS
    for base in dirs:
        candidate = base / filename
        if candidate.is_file():
            return candidate
    return None


def local_photo_path(source: str | None, url: str) -> Path:
    """Where the harvest put a listing photo: ``downloads/<source>/media/<prefix><basename>``."""
    source = source or "eauctionsindia"
    return DOWNLOADS_DIR / source / "media" / f"{ID_PREFIX.get(source, '')}{filename_from_url(url)}"


def upsert_document(
    *,
    auction_id: str,
    filename: str,
    storage_key: str,
    public_url: str,
    content_type: str,
    doc_type: str,
    source: str | None = None,
    doc_role: str | None = None,
) -> None:
    """Idempotently upsert the canonical :Document for ``filename`` (one node
    per filename across auctions) and link it to ``auction_id``. ``source``
    and ``doc_role`` are only ever filled in, never overwritten."""
    run_query(UPSERT_DOC_CYPHER, {
        "auction_id":   auction_id,
        "filename":     filename,
        "storage_key":  storage_key,
        "public_url":   public_url,
        "content_type": content_type,
        "doc_type":     doc_type,
        "source":       source,
        "doc_role":     doc_role,
    })


def process_auction(
    auction_id: str,
    filenames: list[str],
    *,
    dry_run: bool,
    result: UploadResult,
    source: str | None = None,
    roles: list[str] | None = None,
) -> None:
    """``roles`` is the loader's ``document_roles``, parallel to ``filenames``;
    a shorter or missing list leaves ``doc_role`` unset for the rest."""
    roles = roles or []
    for idx, raw in enumerate(filenames):
        filename = (raw or "").strip()
        if not filename or filename.upper() == "N/A":
            continue
        doc_role = roles[idx] if idx < len(roles) else None

        canonical = lookup_canonical(filename)
        if canonical:
            key = canonical["storage_key"]
            public_url = canonical["public_url"]
            content_type = canonical.get("content_type") or storage.guess_content_type(filename)
            doc_type = storage.doc_type_from_content_type(content_type)
            if dry_run:
                print(f"  [dry-run] would reuse canonical for {auction_id} :: {filename} -> {key}")
                continue
            # Only reuse a canonical whose object is actually in R2. A dangling
            # canonical (file deleted, or never uploaded under that key) would
            # otherwise propagate a 404ing public_url to this auction. If the
            # object is gone, fall through to the local-upload path below and
            # re-upload under this auction's own key.
            if storage.exists(key):
                try:
                    upsert_document(
                        auction_id=auction_id,
                        filename=filename,
                        storage_key=key,
                        public_url=public_url,
                        content_type=content_type,
                        doc_type=doc_type,
                        source=source,
                        doc_role=doc_role,
                    )
                    result.reused_canonical += 1
                    result.graph_updates += 1
                    print(f"  [reused] {auction_id} :: {filename} -> {public_url}")
                except Exception as e:
                    result.errors += 1
                    print(f"  [error] {auction_id} :: {filename}: {e}")
                continue
            print(f"  [canonical-missing] {auction_id} :: {filename}: "
                  f"object gone from R2 ({key}); re-uploading from local")

        local = locate_local_file(filename, source)
        if local is None:
            print(f"  [missing] {auction_id} :: {filename}")
            result.skipped_missing += 1
            continue

        key = storage.object_key(auction_id, filename)
        content_type = storage.guess_content_type(filename)
        doc_type = storage.doc_type_from_content_type(content_type)

        if dry_run:
            print(f"  [dry-run] would upload {local} -> {key} ({content_type})")
            continue

        try:
            if storage.exists(key):
                public_url = storage.public_url_for(key)
                result.skipped_exists += 1
                action = "exists"
            else:
                public_url = storage.upload_file(local, key, content_type)
                result.uploaded += 1
                action = "uploaded"

            upsert_document(
                auction_id=auction_id,
                filename=filename,
                storage_key=key,
                public_url=public_url,
                content_type=content_type,
                doc_type=doc_type,
                source=source,
                doc_role=doc_role,
            )
            result.graph_updates += 1
            print(f"  [{action}] {auction_id} :: {filename} -> {public_url}")
        except Exception as e:
            result.errors += 1
            print(f"  [error] {auction_id} :: {filename}: {e}")


def process_photo(auction_id: str, source: str | None, url: str, *, dry_run: bool, result: UploadResult) -> None:
    """Mirror one harvested photo to R2 and record its fingerprint on the
    ``:Media`` node. Keyed by content, so a re-listed photo is one object."""
    local = local_photo_path(source, url)
    if not local.is_file():
        print(f"  [photo-missing] {auction_id} :: {url} (expected {local})")
        result.photos_missing += 1
        return
    data = local.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    key = storage.media_object_key(auction_id, sha, local.suffix)
    content_type = storage.guess_content_type(local.name)
    if dry_run:
        print(f"  [dry-run] would mirror {local} -> {key} ({content_type})")
        return
    try:
        if storage.exists(key):
            public_url = storage.public_url_for(key)
            result.photos_exist += 1
            action = "photo-exists"
        else:
            public_url = storage.upload_file(local, key, content_type)
            result.photos_uploaded += 1
            action = "photo"
        run_query(UPDATE_MEDIA_CYPHER, {"url": url, "sha": sha, "key": key,
                                        "public_url": public_url, "content_type": content_type})
        result.graph_updates += 1
        print(f"  [{action}] {auction_id} :: {url} -> {public_url}")
    except Exception as e:
        result.errors += 1
        print(f"  [error] {auction_id} :: {url}: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="List intended uploads without touching R2 or Neo4j.")
    parser.add_argument("--auction-id", default=None,
                        help="Only process this single auction_id (handy for testing).")
    parser.add_argument("--no-photos", action="store_true",
                        help="Skip mirroring listing photos to R2.")
    args = parser.parse_args()

    if not args.dry_run:
        # Fail fast if R2 isn't configured rather than halfway through.
        try:
            storage.r2_client()
        except storage.R2ConfigError as e:
            sys.exit(f"R2 is not configured: {e}")

    properties = run_query(FETCH_CYPHER, {"auction_id": args.auction_id})
    photos = [] if args.no_photos else run_query(FETCH_MEDIA_CYPHER, {"auction_id": args.auction_id})
    if not properties and not photos:
        print("No properties with downloads_list, and no photos to mirror.")
        return

    print(f"Processing {len(properties)} auction properties...")
    result = UploadResult()
    for row in properties:
        process_auction(
            auction_id=row["auction_id"],
            filenames=row["downloads_list"] or [],
            dry_run=args.dry_run,
            result=result,
            source=row.get("source"),
            roles=row.get("document_roles") or [],
        )
    if photos:
        print(f"Mirroring {len(photos)} photos...")
        for row in photos:
            process_photo(row["auction_id"], row.get("source"), row["url"], dry_run=args.dry_run, result=result)

    print("\n" + "=" * 50)
    print(f"  Uploaded         : {result.uploaded}")
    print(f"  Already in R2    : {result.skipped_exists}")
    print(f"  Reused canonical : {result.reused_canonical}")
    print(f"  Missing locally  : {result.skipped_missing}")
    print(f"  Photos mirrored  : {result.photos_uploaded} (+{result.photos_exist} already in R2, {result.photos_missing} missing locally)")
    print(f"  Graph upserts    : {result.graph_updates}")
    print(f"  Errors           : {result.errors}")
    print("=" * 50)


if __name__ == "__main__":
    main()
