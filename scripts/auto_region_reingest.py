"""
scripts/auto_region_reingest.py
-------------------------------
Batch remediation for health-flagged notices: auto-detect horizontal regions
(prose / ruled grid / footer) with :mod:`pipeline.region_detect`, OCR each
region separately through MinerU, merge the per-region blocks back into one
document, and persist — but ONLY when the merged result's OCR-health score
beats the document's current score. Runs entirely over HTTPS (Neo4j HTTP
Query API + MinerU API + R2), so it works from Bolt-firewalled environments.

Safety model:
  * no confident region split           → document untouched
  * any region's OCR fails              → document untouched
  * merged health ≤ current health      → document untouched (gated)
  * persisted docs also get their auto-detected ``crop_regions`` saved, so
    the annotator shows the bands and a reviewer can adjust + re-run.

Usage:
    python -m scripts.auto_region_reingest --dry-run          # detect only
    python -m scripts.auto_region_reingest --limit 3          # pilot
    python -m scripts.auto_region_reingest                    # all flagged
    python -m scripts.auto_region_reingest --files a.jpg b.jpg
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import time
import urllib.parse
import urllib.request

from api.review.blocks import _clean_crop_regions, _merge_region_blocks
from pipeline.mineru import parse_mineru_content_list, safe_cache_name
from pipeline.mineru_api import (
    archive_zip_to_r2, download_zip, parse_zip_payload,
    poll, request_batch, upload_files,
)
from pipeline.ocr_health import score_ocr_health
from pipeline.region_detect import detect_regions

SOURCE_PROXY = os.environ.get(
    "NOTICE_SOURCE_PROXY",
    "https://auction-api-w68b.onrender.com/review/notice/{}/source")
DOCS_PER_MINERU_BATCH = 4        # regions of ~4 docs per batch (8-12 files)


# ── Neo4j over HTTPS ────────────────────────────────────────────────────────

def _endpoint() -> tuple[str, str]:
    host = os.environ["NEO4J_URI"].split("//", 1)[1].rstrip("/")
    db = os.environ.get("NEO4J_DATABASE", "neo4j")
    auth = base64.b64encode(
        f'{os.environ["NEO4J_USERNAME"]}:{os.environ["NEO4J_PASSWORD"]}'.encode()
    ).decode()
    return f"https://{host}/db/{db}/query/v2", auth


def nq(statement: str, parameters: dict | None = None) -> list[list]:
    url, auth = _endpoint()
    body = json.dumps({"statement": statement,
                       "parameters": parameters or {}}).encode()
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Authorization": "Basic " + auth,
                         "Content-Type": "application/json",
                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)["data"]["values"]
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


# ── source fetch + region detection ─────────────────────────────────────────

def fetch_source(filename: str) -> tuple[bytes, str]:
    url = SOURCE_PROXY.format(urllib.parse.quote(filename, safe=""))
    with urllib.request.urlopen(url, timeout=90) as r:
        return r.read(), (r.headers.get("content-type") or "")


def to_page_png(data: bytes, content_type: str, filename: str) -> bytes | None:
    """Raster page-1 bytes for detection/cropping; None for multi-page PDFs."""
    if "pdf" in content_type or filename.lower().endswith(".pdf"):
        import fitz  # type: ignore
        doc = fitz.open(stream=data, filetype="pdf")
        try:
            if doc.page_count > 1:
                return None
            return doc[0].get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")
        finally:
            doc.close()
    return data


# ── persist (mirrors api.review.blocks._persist_reingest_result over HTTP) ──

PERSIST = """
MATCH (d:Document {filename: $filename})
SET d.markdown            = $markdown,
    d.blocks              = $blocks_json,
    d.markdown_raw        = $markdown,
    d.blocks_raw          = $blocks_raw,
    d.markdown_raw_at     = datetime(),
    d.crop_regions        = $crop_regions,
    d.crop_regions_set_at = datetime(),
    d.blocks_revision     = coalesce(d.blocks_revision, 0) + 1,
    d.markdown_loaded_at  = datetime(),
    d.markdown_source     = 'mineru',
    d.markdown_model      = 'mineru-vlm-autoregion',
    d.markdown_verified_at = NULL,
    d.markdown_verified_by = NULL,
    d.markdown_quality     = NULL,
    d.ocr_health_score     = $health_score,
    d.ocr_health_flags     = $health_flags,
    d.ocr_health_at        = datetime()
"""


def process_doc(doc: dict, *, dry_run: bool) -> dict:
    """Detect → OCR regions → merge → gate → persist. Returns a report row."""
    fn, fp = doc["filename"], doc["file_path"]
    cur_health = doc["score"]
    out = {"filename": fn, "old_health": cur_health}

    data, ct = fetch_source(fn)
    png = to_page_png(data, ct, fn)
    if png is None:
        out["status"] = "skip-multipage-pdf"
        return out
    regions = detect_regions(png)
    if not regions:
        out["status"] = "no-split"
        return out
    regions = _clean_crop_regions(regions)
    out["regions"] = len(regions)
    if dry_run:
        out["status"] = "would-ocr"
        return out

    # Crop each region and ship the whole doc's regions as one item set.
    from pipeline.reextract import _image_crop_to_png
    import tempfile
    from pathlib import Path

    items, tmps = [], []
    try:
        for i, region in enumerate(regions):
            crop = _image_crop_to_png(png, region["bbox"])
            fd, name = tempfile.mkstemp(suffix=".png",
                                        prefix=f"autoregion_r{i}_")
            with os.fdopen(fd, "wb") as f:
                f.write(crop)
            tmps.append(Path(name))
            items.append({"filename": f"{Path(fn).stem}_r{i}.png",
                          "file_path": f"{fp}::r{i}",
                          "disk_path": Path(name)})

        batch_id, urls = request_batch(items)
        upload_files(items, urls)
        rows = poll(batch_id, timeout_s=420)
        by_id = {r.get("data_id"): r for r in rows}

        per_region, mds, raws = [], [], []
        img_map: dict = {}
        for i, (region, item) in enumerate(zip(regions, items)):
            row = by_id.get(safe_cache_name(item["file_path"])[:128])
            if row is None or row.get("state") != "done":
                raise RuntimeError(
                    f"region {i + 1}: {row.get('err_msg') if row else 'no row'}")
            zb = download_zip(row.get("full_zip_url") or "")
            if not zb:
                raise RuntimeError(f"region {i + 1}: zip download failed")
            meta = archive_zip_to_r2(item["file_path"], zb)
            img_map.update(meta.get("img_map") or {})
            md, blocks_raw = parse_zip_payload(zb)
            if blocks_raw is None:
                raise RuntimeError(f"region {i + 1}: no content-list")
            mds.append((md or "").strip())
            raws.append(blocks_raw)
            per_region.append(
                (region, parse_mineru_content_list(blocks_raw, img_map=img_map)))
    finally:
        for p in tmps:
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    blocks = _merge_region_blocks(per_region, page=regions[0]["page"])
    import secrets
    for b in blocks:
        if not b.get("id"):
            b["id"] = f"blk_{secrets.token_hex(6)}"
    new_md = "\n\n".join(m for m in mds if m)
    health = score_ocr_health(new_md)
    out["new_health"] = health["score"]
    out["new_flags"] = health["flags"]

    # The gate: only persist a strictly better document.
    if health["score"] is None or (cur_health is not None
                                   and health["score"] <= cur_health):
        out["status"] = "gated"
        return out

    nq(PERSIST, {
        "filename": fn,
        "markdown": new_md,
        "blocks_json": json.dumps(
            {"schema_version": 1, "blocks": blocks}, ensure_ascii=False),
        "blocks_raw": json.dumps(
            [b for lst in raws for b in lst], ensure_ascii=False),
        "crop_regions": json.dumps(regions, ensure_ascii=False),
        "health_score": health["score"],
        "health_flags": health["flags"],
    })
    _rescore_coverage(fp, new_md)
    out["status"] = "persisted"
    out["blocks"] = len(blocks)
    return out


def _rescore_coverage(fp: str, markdown: str) -> None:
    """Refresh markdown_quality_score for a changed doc (best-effort).

    Same blend as pipeline.score_markdown._score_one, fed over HTTP.
    """
    try:
        from pipeline.score_markdown import _score_one
        rows = nq("""
            MATCH (d:Document {file_path: $fp})
            OPTIONAL MATCH (d)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
            OPTIONAL MATCH (a)-[:HAS_BORROWER]->(b:Borrower)
            WITH a, collect(DISTINCT b.name) AS borrowers
            WHERE a IS NOT NULL
            RETURN a.reserve_price_num, a.website_description, borrowers
        """, {"fp": fp})
        props = [{"reserve_price": r[0], "website_description": r[1],
                  "borrowers": r[2]} for r in rows]
        score = _score_one(markdown, props)
        nq("""
            MATCH (d:Document {file_path: $fp})
            SET d.markdown_quality_score = $score,
                d.markdown_quality_scored_at = datetime()
        """, {"fp": fp, "score": score})
    except Exception as e:
        print(f"    [coverage-rescore-fail] {type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="detect + report only; no OCR, no writes")
    ap.add_argument("--limit", type=int, default=None,
                    help="process at most N docs (worst health first)")
    ap.add_argument("--files", nargs="*", default=None,
                    help="restrict to specific filenames")
    args = ap.parse_args()

    where = "size(coalesce(d.ocr_health_flags,[])) > 0"
    params: dict = {}
    if args.files:
        where += " AND d.filename IN $files"
        params["files"] = args.files
    rows = nq(f"""
        MATCH (d:Document) WHERE {where} AND coalesce(d.rotation, 0) = 0
        RETURN d.filename, d.file_path, d.ocr_health_score
        ORDER BY d.ocr_health_score ASC
    """, params)
    docs = [{"filename": r[0], "file_path": r[1], "score": r[2]} for r in rows]
    if args.limit:
        docs = docs[: args.limit]
    print(f"processing {len(docs)} flagged docs (dry_run={args.dry_run})")

    from collections import Counter
    tally: Counter = Counter()
    for i, doc in enumerate(docs, 1):
        try:
            rep = process_doc(doc, dry_run=args.dry_run)
        except Exception as e:
            rep = {"filename": doc["filename"],
                   "status": f"err-{type(e).__name__}", "detail": str(e)[:120]}
        tally[rep["status"]] += 1
        extra = ""
        if rep.get("new_health") is not None:
            extra = (f"  health {rep.get('old_health')} -> {rep['new_health']}"
                     f"  flags={rep.get('new_flags')}")
        if rep.get("blocks"):
            extra += f"  blocks={rep['blocks']}"
        print(f"  [{i}/{len(docs)}] {rep['status']:<18} "
              f"{rep['filename'][:48]}{extra}", flush=True)

    print("\nTALLY:", dict(tally))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
