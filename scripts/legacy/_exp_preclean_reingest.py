"""One-off: re-ingest a Document with pre-cleaning, so the cleaned OCR
output shows up in the review UI.

Mirrors api/review/blocks.py::reingest_notice but inserts a pre-clean
step (LANCZOS upscale + unsharp mask) before shipping to MinerU. Writes
the cleaned MinerU output to the same caches and Neo4j fields the UI
reads, bumping ``Document.blocks_revision`` so the reviewer's Reload
button picks it up.

Run:
  python -m scripts._exp_preclean_reingest                       # default notice
  python -m scripts._exp_preclean_reingest <filename.jpg>        # any notice
  python -m scripts._exp_preclean_reingest <filename.jpg> --factor 3
"""
from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import time
from pathlib import Path

import requests
from PIL import Image, ImageFilter

from api.neo4j_client import run_query, run_read_query
from pipeline.load_markdowns_to_neo4j import load_blocks_for
from pipeline.mineru import (
    MINERU_SUPPORTED_EXTS,
    find_disk_path,
)
from pipeline.mineru_api import (
    download_and_cache,
    poll,
    request_batch,
    upload_files,
)

DEFAULT_FILENAME = "9b6c180b-0d13-4d2b-ae71-2bb2d373b21517785819725241.jpg"


def fetch_document(filename: str) -> dict:
    rows = run_read_query(
        """
        MATCH (d:Document {filename: $filename})
        RETURN d.file_path  AS file_path,
               d.filename   AS filename,
               d.public_url AS public_url
        """,
        {"filename": filename},
        max_rows=1,
    )
    if not rows:
        raise SystemExit(f"Document not found in Neo4j: {filename}")
    return rows[0]


def resolve_source(filename: str, public_url: str | None) -> tuple[Path, Path | None]:
    """Return (disk_path, tmp_to_delete). Pulls from R2 if not on disk."""
    disk = find_disk_path(filename)
    if disk is not None:
        return disk, None
    if not public_url:
        raise SystemExit(f"No local file and no public_url for {filename}")
    ext = Path(filename).suffix.lower() or ".bin"
    if ext not in MINERU_SUPPORTED_EXTS:
        raise SystemExit(f"Unsupported source extension: {ext}")
    resp = requests.get(public_url, timeout=120, stream=True)
    resp.raise_for_status()
    fd, name = tempfile.mkstemp(suffix=ext, prefix="reingest_")
    p = Path(name)
    with os.fdopen(fd, "wb") as out:
        for chunk in resp.iter_content(65536):
            if chunk:
                out.write(chunk)
    resp.close()
    return p, p


def preclean(src: Path, factor: int) -> Path:
    """LANCZOS upscale + unsharp mask, save as JPEG q=95 tempfile."""
    im = Image.open(src).convert("RGB")
    w, h = im.size
    im = im.resize((w * factor, h * factor), Image.LANCZOS)
    im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    fd, name = tempfile.mkstemp(suffix=".jpg", prefix="preclean_")
    out = Path(name)
    with os.fdopen(fd, "wb") as f:
        im.save(f, format="JPEG", quality=95)
    print(f"  pre-cleaned: {w}x{h} -> {w*factor}x{h*factor}  ({out.stat().st_size//1024} KB)")
    return out


def mineru_ocr(disk: Path, src_filename: str, file_path: str,
               attempts: int = 3) -> tuple[Path, Path]:
    """Submit cleaned image to MinerU, cache md+blocks under canonical file_path.
    Retries the batch on connection resets (same pattern stage1_mineru uses)."""
    mineru_filename = Path(src_filename).stem + "_preclean.jpg"
    item = {"filename": mineru_filename, "file_path": file_path, "disk_path": disk}
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            batch_id, urls = request_batch([item])
            print(f"  attempt {attempt+1}/{attempts}  batch_id={batch_id}")
            upload_files([item], urls)
            results = poll(batch_id, timeout_s=300)
            if not results or results[0].get("state") != "done":
                raise RuntimeError(f"MinerU state != done: {results}")
            zip_url = results[0]["full_zip_url"]
            md_path, blocks_path = download_and_cache(file_path, zip_url)
            if md_path is None or blocks_path is None:
                raise RuntimeError("MinerU returned no markdown or blocks")
            return md_path, blocks_path
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last = e
            wait = 5 * (attempt + 1)
            print(f"  [transient] {type(e).__name__}; retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"MinerU failed after {attempts} attempts: {last}")


def write_to_neo4j(filename: str, file_path: str, md_path: Path) -> int:
    """Write blocks + markdown to Neo4j, bump blocks_revision, clear verification.
    Mirrors the tail of api/review/blocks.py::reingest_notice."""
    blocks = load_blocks_for(file_path) or []
    if not blocks:
        raise SystemExit("No blocks produced -- cannot update review UI.")
    doc = {"schema_version": 1, "blocks": blocks}
    md = md_path.read_text(encoding="utf-8")
    rows = run_query(
        """
        MATCH (d:Document {filename: $filename})
        SET d.markdown             = $markdown,
            d.blocks               = $blocks_json,
            d.blocks_revision      = coalesce(d.blocks_revision, 0) + 1,
            d.markdown_loaded_at   = datetime(),
            d.markdown_source      = 'mineru',
            d.markdown_model       = 'mineru-vlm-preclean2x',
            d.markdown_verified_at = NULL,
            d.markdown_verified_by = NULL,
            d.markdown_quality     = NULL
        RETURN d.blocks_revision AS rev
        """,
        {"filename": filename,
         "markdown": md,
         "blocks_json": json.dumps(doc, ensure_ascii=False)},
    )
    return int(rows[0]["rev"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("filename", nargs="?", default=DEFAULT_FILENAME)
    ap.add_argument("--factor", type=int, default=2, help="upscale factor (default 2)")
    args = ap.parse_args()

    print(f"Filename: {args.filename}")
    doc_row = fetch_document(args.filename)
    fp = doc_row["file_path"]
    print(f"  file_path: {fp}")
    print(f"  public_url: {doc_row.get('public_url')}")

    disk, tmp_to_delete = resolve_source(args.filename, doc_row.get("public_url"))
    print(f"  source on disk: {disk}")
    cleaned: Path | None = None
    try:
        cleaned = preclean(disk, args.factor)
        md_path, blocks_path = mineru_ocr(cleaned, args.filename, fp)
        print(f"  wrote {md_path}")
        print(f"  wrote {blocks_path}")
        new_rev = write_to_neo4j(args.filename, fp, md_path)
        print(f"\nDone. blocks_revision is now {new_rev}.")
        print("Hit Reload in the review UI to see the cleaned output.")
    finally:
        if cleaned is not None:
            try:
                cleaned.unlink()
            except FileNotFoundError:
                pass
        if tmp_to_delete is not None:
            try:
                tmp_to_delete.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()
