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

--reingest POSTs /review/notice/{filename}/reingest on the API after saving
the crop; it needs REVIEW_API_TOKEN (an admin's access token) and optionally
REVIEW_API_BASE (default: the production API).

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
MAX_AUTO_AREA = 0.60
# Tag written alongside the crop so the annotator / audits can tell an
# auto-drawn box from a reviewer's.
CROP_SOURCE = "auto-locate"

REVIEW_API_BASE = os.environ.get(
    "REVIEW_API_BASE", "https://auction-api-w68b.onrender.com")

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".jfif")


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
               d.ocr_health_score
        ORDER BY d.ocr_health_score ASC
        {'LIMIT $lim' if limit else ''}
    """, {**params, **({"lim": limit} if limit else {})})
    return [{"filename": r[0], "file_path": r[1], "blocks_json": r[2],
             "properties": r[3] or [], "health": r[4]} for r in rows]


# ── one document ────────────────────────────────────────────────────────────

def process_doc(doc: dict, *, dry_run: bool, preview_dir: Path | None,
                reingest: bool) -> dict:
    fn = doc["filename"]
    out: dict = {"filename": fn}
    blocks = _parse_doc_blob(doc.get("blocks_json")).get("blocks") or []
    if not blocks:
        out["status"] = "no-blocks"
        return out

    data, ct = fetch_source(fn)
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
            _post_reingest(fn)
            out["status"] = "cropped+reingest"
        except Exception as e:  # noqa: BLE001 — report, don't abort the batch
            out["status"] = "cropped;reingest-failed"
            out["detail"] = f"{type(e).__name__}: {str(e)[:100]}"
    return out


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
                    help="every image notice with blocks and no crop yet")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true",
                    help="also documents that already have a crop / regions")
    ap.add_argument("--dry-run", action="store_true",
                    help="locate + report only; no writes")
    ap.add_argument("--preview", type=Path, default=None, metavar="DIR",
                    help="write DIR/<stem>_crop.png with the box drawn; no writes")
    ap.add_argument("--reingest", action="store_true",
                    help="after saving the crop, POST the API re-ingest")
    args = ap.parse_args()

    docs = select_docs(files=args.files, auto=args.auto,
                       limit=args.limit, force=args.force)
    print(f"locating on {len(docs)} docs "
          f"(dry_run={args.dry_run or args.preview is not None})")
    tally: Counter = Counter()
    for i, doc in enumerate(docs, 1):
        try:
            rep = process_doc(doc, dry_run=args.dry_run,
                              preview_dir=args.preview, reingest=args.reingest)
        except Exception as e:  # noqa: BLE001 — one bad doc must not stop the batch
            rep = {"filename": doc["filename"],
                   "status": f"err-{type(e).__name__}", "detail": str(e)[:120]}
        tally[rep["status"]] += 1
        extra = ""
        if rep.get("bbox"):
            extra = (f"  bbox={rep['bbox']} area={rep['area']} "
                     f"score={rep['score']} via={','.join(rep['matched'])}")
        if rep.get("detail"):
            extra += f"  {rep['detail']}"
        if rep.get("preview"):
            extra += f"  -> {rep['preview']}"
        print(f"  [{i}/{len(docs)}] {rep['status']:<18} "
              f"{rep['filename'][:48]}{extra}", flush=True)
    print("\nTALLY:", dict(tally))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
