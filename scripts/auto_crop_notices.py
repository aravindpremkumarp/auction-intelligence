"""
scripts/auto_crop_notices.py
----------------------------
Draw the annotator's crop box automatically for notices that were uploaded
as a whole newspaper page.

Some portals attach the entire classifieds page (ten banks' notices on one
scan, e.g. ``JM17727039268646.jpg``) as a property's sale notice. Reviewers
have been cropping those by hand in the annotator. This script does the same
thing with :mod:`pipeline.notice_locate`: it scores the page's stored OCR
blocks against the linked property (reserve price, borrower, bank, auction
date, website description), grows a cluster around the best match, and
snaps that cluster to the notice's printed frame or the white gutter around
it. The result is saved exactly where a reviewer's hand-drawn crop goes
(``d.crop_bbox`` / ``d.crop_page``), so the existing re-ingest path OCRs
only the notice, and the annotator shows the box for the reviewer to adjust.

Safety model:
  * no property hints / no matching block / OCR collapsed the page into one
    block                                    → document untouched
  * located box covers > MAX_AUTO_AREA of the page (the notice IS the page)
                                             → document untouched
  * document already has a crop or regions   → untouched unless --force
  * --dry-run / --preview never write. --preview also saves a PNG per doc
    with the crop drawn in red (and the block cluster in blue) so the boxes
    can be eyeballed before anything is persisted.

Runs entirely over HTTPS (Neo4j HTTP Query API + the API's source proxy), so
it works from Bolt-firewalled environments — same pattern as
scripts/auto_region_reingest.py.

Usage:
    python -m scripts.auto_crop_notices --files JM17727039268646.jpg --preview out/
    python -m scripts.auto_crop_notices --files JM17727039268646.jpg          # persist crop
    python -m scripts.auto_crop_notices --auto --limit 20 --dry-run          # scan candidates
    python -m scripts.auto_crop_notices --auto --limit 20 --reingest         # crop + re-OCR

--reingest re-OCRs the cropped notice after saving the crop. With
REVIEW_API_TOKEN (an admin's access token; REVIEW_API_BASE defaults to the
production API) it POSTs /review/notice/{filename}/reingest and the API does
the work. Without it, the same steps run here over HTTPS: crop the page,
Datalab (DATALAB_API_KEY) on the crop, remap the blocks to full-page
coordinates, persist with the same fields ``api.review.blocks`` writes, and
re-score coverage + OCR health — the Bolt-free mirror of ``reingest_notice``.

Auth: NEO4J_URI/USERNAME/PASSWORD(/DATABASE) in the environment.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from api.review.blocks import _parse_doc_blob
from pipeline.notice_locate import locate_notice
from scripts.auto_region_reingest import fetch_source, to_page_png
from scripts.score_ink_coverage import nq

# A located box larger than this is the whole page: nothing to crop away.
# JM17727039268646 — the notice is the bottom 70% of the page under two
# other banks' notices — locates at 0.65 and is exactly the case to crop.
MAX_AUTO_AREA = 0.85
# Tag written alongside the crop so the annotator / audits can tell an
# auto-drawn box from a reviewer's.
CROP_SOURCE = "auto-locate"

REVIEW_API_BASE = os.environ.get(
    "REVIEW_API_BASE", "https://auction-api-w68b.onrender.com")

# Single-page PDFs render to one page image; multi-page ones are skipped
# at process time (see to_page_png).
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".jfif", ".pdf")


# ── selection ───────────────────────────────────────────────────────────────

def select_docs(*, files: list[str] | None, auto: bool,
                limit: int | None, force: bool) -> list[dict]:
    """Documents to try, with their blocks and linked-property hints."""
    where = ["d.blocks IS NOT NULL", "coalesce(d.rotation, 0) = 0"]
    params: dict = {}
    if files:
        where.append("d.filename IN $files")
        params["files"] = files
    elif auto:
        where.append("any(ext IN $exts WHERE toLower(d.filename) ENDS WITH ext)")
        params["exts"] = list(IMAGE_EXTS)
    else:
        raise SystemExit("pass --files ... or --auto")
    if not force:
        where.append("d.crop_bbox IS NULL AND d.crop_regions IS NULL")
    rows = nq(f"""
        MATCH (d:Document) WHERE {' AND '.join(where)}
        MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d)
        OPTIONAL MATCH (a)-[:HAS_BORROWER]->(b:Borrower)
        OPTIONAL MATCH (a)-[:CONDUCTED_BY]->(bank:Bank)
        OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(city:City)
        OPTIONAL MATCH (a)-[:LOCATED_IN_AREA]->(area:Area)
        WITH d, a, bank, city, area, collect(DISTINCT b.name) AS borrowers
        WITH d, collect({{
            reserve_price:       a.reserve_price_num,
            borrowers:           borrowers,
            bank:                bank.name,
            auction_start:       toString(a.auction_start_dt),
            website_description: a.website_description,
            city:                city.name,
            area:                area.name
        }}) AS props
        RETURN d.filename, d.file_path, d.blocks, props,
               d.ocr_health_score, d.notice_type, d.public_url
        ORDER BY d.ocr_health_score ASC
        {'LIMIT $lim' if limit else ''}
    """, {**params, **({"lim": limit} if limit else {})})
    return [{"filename": r[0], "file_path": r[1], "blocks_json": r[2],
             "properties": r[3] or [], "health": r[4], "notice_type": r[5],
             "public_url": r[6]}
            for r in rows]


def _fetch(doc: dict) -> tuple[bytes, str]:
    """Source bytes + content type: straight from R2 when the Document has
    a public_url (no API hop), else through the API's source proxy."""
    url = doc.get("public_url")
    if url:
        # R2's public host answers 403 to a request with no User-Agent.
        req = urllib.request.Request(
            url, headers={"User-Agent": "auction-intelligence/auto-crop"})
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.read(), (r.headers.get("content-type") or "")
    return fetch_source(doc["filename"])


# ── one document ────────────────────────────────────────────────────────────

def process_doc(doc: dict, *, dry_run: bool, preview_dir: Path | None,
                reingest: bool) -> dict:
    fn = doc["filename"]
    out: dict = {"filename": fn}
    blocks = _parse_doc_blob(doc.get("blocks_json")).get("blocks") or []
    if not blocks:
        out["status"] = "no-blocks"
        return out

    data, ct = _fetch(doc)
    png = to_page_png(data, ct, fn)
    if png is None:
        out["status"] = "skip-multipage-pdf"
        return out

    res = locate_notice(png, blocks, doc.get("properties") or [])
    if res is None:
        out["status"] = "no-anchor"
        return out
    out.update({"bbox": res["bbox"], "score": res["score"],
                "matched": res["matched"], "snapped": res["snapped"]})
    x0, y0, x1, y1 = res["bbox"]
    area = (x1 - x0) * (y1 - y0)
    out["area"] = round(area, 3)

    if preview_dir is not None:
        preview_dir.mkdir(parents=True, exist_ok=True)
        path = preview_dir / f"{Path(fn).stem}_crop.png"
        path.write_bytes(_draw_preview(png, res))
        out["preview"] = str(path)

    if area > MAX_AUTO_AREA:
        out["status"] = "fills-page"
        return out
    if dry_run or preview_dir is not None:
        out["status"] = "would-crop"
        return out

    nq("""
        MATCH (d:Document {filename: $filename})
        SET d.crop_bbox        = $bbox,
            d.crop_page        = $page,
            d.crop_bbox_set_at = datetime(),
            d.crop_source      = $source,
            d.crop_auto_score  = $score,
            d.crop_auto_matched = $matched
    """, {"filename": fn, "bbox": res["bbox"], "page": res["page"],
          "source": CROP_SOURCE, "score": res["score"],
          "matched": res["matched"]})
    out["status"] = "cropped"

    if reingest:
        try:
            if os.environ.get("REVIEW_API_TOKEN"):
                _post_reingest(fn)
                out["status"] = "cropped+reingest-queued"
            else:
                rep = _reingest_over_https(doc, png, res)
                out.update(rep)
                out["status"] = "cropped+reingested"
        except Exception as e:  # noqa: BLE001 — report, don't abort the batch
            out["status"] = "cropped;reingest-failed"
            out["detail"] = f"{type(e).__name__}: {str(e)[:100]}"
    return out


# Mirrors api.review.blocks._persist_reingest_result, plus the health fields
# pipeline.ocr_health.score_freshly_loaded would stamp afterwards.
PERSIST_REINGEST = """
MATCH (d:Document {filename: $filename})
SET d.markdown            = $markdown,
    d.blocks              = $blocks_json,
    d.markdown_raw        = $markdown,
    d.blocks_raw          = coalesce($blocks_raw, d.blocks_raw),
    d.markdown_raw_at     = datetime(),
    d.blocks_revision     = coalesce(d.blocks_revision, 0) + 1,
    d.markdown_loaded_at  = datetime(),
    d.markdown_source     = 'datalab',
    d.markdown_model      = $model,
    d.parse_quality_score = coalesce($parse_quality, d.parse_quality_score),
    d.parse_quality_at    = CASE WHEN $parse_quality IS NULL
                                THEN d.parse_quality_at ELSE datetime() END,
    d.markdown_verified_at = NULL,
    d.markdown_verified_by = NULL,
    d.markdown_quality     = NULL,
    d.ocr_health_score     = $health_score,
    d.ocr_health_flags     = $health_flags,
    d.ocr_health_at        = datetime()
"""


def _reingest_over_https(doc: dict, page_png: bytes, res: dict) -> dict:
    """Bolt-free re-ingest of the cropped notice through Datalab.

    Same steps as ``api.review.blocks.reingest_notice`` on its single-crop
    Datalab path: crop → ``datalab_api.run_and_cache`` → ``load_blocks_for``
    → remap block bboxes from crop coords into full-page coords (clamped to
    the crop) and retag the page → persist → re-score. A run that yields no
    blocks raises, so the existing block layer is never replaced by nothing.
    """
    import secrets
    import tempfile
    from pathlib import Path

    from pipeline import datalab_api
    from pipeline.config import datalab_mode_for
    from pipeline.load_markdowns_to_neo4j import load_blocks_for, read_parse_quality
    from pipeline.ocr_health import score_ocr_health
    from pipeline.reextract import _image_crop_to_png
    from scripts.auto_region_reingest import _rescore_coverage

    fn, fp = doc["filename"], doc["file_path"]
    bbox, page = res["bbox"], res["page"]
    mode = datalab_mode_for(doc.get("notice_type"))
    crop = _image_crop_to_png(page_png, bbox)
    fd, name = tempfile.mkstemp(suffix=".png", prefix="autocrop_reingest_")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(crop)
        md_path, blocks_path = datalab_api.run_and_cache(fp, tmp, mode=mode)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    blocks = load_blocks_for(fp, img_map={})
    if not blocks:
        raise RuntimeError("datalab produced no parseable blocks; kept existing layer")

    cx0, cy0, cx1, cy1 = bbox
    cw, ch = cx1 - cx0, cy1 - cy0
    for blk in blocks:
        bx0, by0, bx1, by1 = blk["bbox"]
        blk["bbox"] = [
            min(max(cx0 + bx0 * cw, cx0), cx1),
            min(max(cy0 + by0 * ch, cy0), cy1),
            min(max(cx0 + bx1 * cw, cx0), cx1),
            min(max(cy0 + by1 * ch, cy0), cy1),
        ]
        blk["page"] = page
        if not blk.get("id"):
            blk["id"] = f"blk_{secrets.token_hex(6)}"

    markdown = md_path.read_text(encoding="utf-8")
    try:
        blocks_raw = blocks_path.read_text(encoding="utf-8") if blocks_path else None
    except (OSError, UnicodeDecodeError):
        blocks_raw = None
    health = score_ocr_health(markdown)
    nq(PERSIST_REINGEST, {
        "filename": fn,
        "markdown": markdown,
        "blocks_json": json.dumps({"schema_version": 1, "blocks": blocks},
                                  ensure_ascii=False),
        "blocks_raw": blocks_raw,
        "model": f"datalab-{mode}",
        "parse_quality": read_parse_quality(fp),
        "health_score": health["score"],
        "health_flags": health["flags"],
    })
    _rescore_coverage(fp, markdown)
    return {"blocks": len(blocks), "health": health["score"],
            "flags": health["flags"], "md_chars": len(markdown)}


def _draw_preview(png: bytes, res: dict) -> bytes:
    from PIL import Image, ImageDraw
    im = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = im.size
    d = ImageDraw.Draw(im)
    ax = res["anchor_bbox"]
    d.rectangle([ax[0] * w, ax[1] * h, ax[2] * w, ax[3] * h],
                outline=(40, 90, 255), width=max(2, w // 500))
    bx = res["bbox"]
    d.rectangle([bx[0] * w, bx[1] * h, bx[2] * w, bx[3] * h],
                outline=(230, 30, 30), width=max(3, w // 300))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _post_reingest(filename: str) -> None:
    token = os.environ.get("REVIEW_API_TOKEN")
    if not token:
        raise RuntimeError("REVIEW_API_TOKEN not set")
    url = (f"{REVIEW_API_BASE.rstrip('/')}/review/notice/"
           f"{urllib.parse.quote(filename, safe='')}/reingest")
    req = urllib.request.Request(
        url, data=b"{}", method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        if r.status not in (200, 202):
            raise RuntimeError(f"HTTP {r.status}")


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--files", nargs="*", default=None,
                    help="restrict to specific filenames")
    ap.add_argument("--auto", action="store_true",
                    help="every image / single-page PDF notice with blocks "
                         "and no crop yet")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true",
                    help="also documents that already have a crop / regions")
    ap.add_argument("--dry-run", action="store_true",
                    help="locate + report only; no writes")
    ap.add_argument("--preview", type=Path, default=None, metavar="DIR",
                    help="write DIR/<stem>_crop.png with the box drawn; no writes")
    ap.add_argument("--reingest", action="store_true",
                    help="after saving the crop, re-OCR the cropped notice")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="documents processed in parallel (source fetch + "
                         "locate are independent per doc)")
    args = ap.parse_args()

    docs = select_docs(files=args.files, auto=args.auto,
                       limit=args.limit, force=args.force)
    print(f"locating on {len(docs)} docs "
          f"(dry_run={args.dry_run or args.preview is not None})")
    tally: Counter = Counter()

    def _one(doc: dict) -> dict:
        try:
            return process_doc(doc, dry_run=args.dry_run,
                               preview_dir=args.preview, reingest=args.reingest)
        except Exception as e:  # noqa: BLE001 — one bad doc must not stop the batch
            return {"filename": doc["filename"],
                    "status": f"err-{type(e).__name__}", "detail": str(e)[:120]}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        reports = pool.map(_one, docs)
        for i, rep in enumerate(reports, 1):
            tally[rep["status"]] += 1
            _print_row(i, len(docs), rep)
    print("\nTALLY:", dict(tally))
    return 0


def _print_row(i: int, n: int, rep: dict) -> None:
    extra = ""
    if rep.get("bbox"):
        extra = (f"  bbox={rep['bbox']} area={rep['area']} "
                 f"score={rep['score']} via={','.join(rep['matched'])}")
    if rep.get("blocks"):
        extra += (f"  blocks={rep['blocks']} health={rep.get('health')} "
                  f"flags={rep.get('flags')} md={rep.get('md_chars')}ch")
    if rep.get("detail"):
        extra += f"  {rep['detail']}"
    if rep.get("preview"):
        extra += f"  -> {rep['preview']}"
    print(f"  [{i}/{n}] {rep['status']:<18} "
          f"{rep['filename'][:48]}{extra}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
