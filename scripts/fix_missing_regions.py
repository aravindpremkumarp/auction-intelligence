"""
scripts/fix_missing_regions.py
------------------------------
Recover the content behind a ``missing-region`` flag by re-OCRing just the gap.

``pipeline/ink_coverage.py`` reports not only how much ink no block covers but
*where* — ``details.patch_bbox``, the largest connected unread patch. That is
exactly the crop the parser needs a second look at, so this script:

  1. recomputes coverage (blocks and image are already stored, no API call) to
     locate the patch;
  2. crops it out of the source image with a small margin;
  3. sends only that crop to Datalab — a small region on its own is a far
     easier parse than the full dense page, the same principle
     ``scripts/auto_region_reingest.py`` uses for its band splits;
  4. merges the recovered blocks into the document, regenerates the markdown,
     and re-measures.

Unlike ``scripts/backfill_blocks_datalab.py``, this **does** rewrite
``d.markdown`` — that is the whole point, since the stored text is missing the
content in that patch. Consequences, handled explicitly:

  * ``d.markdown_verified_at`` / ``markdown_quality`` are cleared. A human
    signed off on text that is now replaced, and silently keeping that sign-off
    would be worse than asking for it again.
  * ``d.extraction_stale_at`` is stamped. langextract read the old markdown, so
    its output no longer reflects the notice; ``extraction_json`` is left in
    place (stale beats absent) and the affected file_paths are printed for a
    follow-up extraction run.

Safety: a document is written only when the new parse is BOTH more complete
(coverage improves by ``MIN_GAIN``) and not shorter than what it replaces
(``MIN_KEEP_RATIO``). A crop that comes back empty or garbled therefore leaves
the notice exactly as it was.

Usage:
    python -m scripts.fix_missing_regions --dry-run              # select + preview
    python -m scripts.fix_missing_regions --upcoming --limit 5   # pilot
    python -m scripts.fix_missing_regions --upcoming             # all upcoming
Options: --concurrency 6  --margin 0.01

Auth: NEO4J_URI/USERNAME/PASSWORD(/DATABASE) + DATALAB_API_KEY.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import os
import secrets
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from api.review.blocks import _merge_region_blocks
from pipeline import datalab_api
from pipeline.config import datalab_mode_for
from pipeline.datalab import parse_datalab_blocks
from pipeline.ink_coverage import score_ink_coverage
from pipeline.mineru import assemble_markdown
from pipeline.ocr_health import score_ocr_health
from pipeline.reextract import _image_crop_to_png
from scripts.score_ink_coverage import nq


# Coverage must improve by at least this much for the rewrite to be worth it.
MIN_GAIN = 0.03
# ...and the new markdown must retain at least this share of the old length, so
# a crop that somehow degrades the whole-page parse can never shrink the text.
MIN_KEEP_RATIO = 0.95
# Grow the crop slightly: a patch boundary sits mid-glyph, and OCR of a line
# sliced in half is worse than useless.
DEFAULT_MARGIN = 0.01


def select_targets(*, upcoming: bool, limit: int | None) -> list[dict]:
    """Documents flagged ``missing-region``, optionally only live auctions."""
    if upcoming:
        today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        match = "MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)"
        extra = "AND a.auction_start_dt >= datetime($t)"
        params = {"t": f"{today}T00:00:00Z"}
    else:
        match, extra, params = "MATCH (d:Document)", "", {}
    rows = nq(
        f"""
        {match}
        WHERE 'missing-region' IN coalesce(d.ocr_health_flags, [])
          AND d.public_url IS NOT NULL AND d.public_url <> ''
          AND coalesce(d.blocks_source,'') <> 'datalab-backfill'
          {extra}
        WITH DISTINCT d
        RETURN d.file_path, d.filename, coalesce(d.notice_type,'unknown'),
               d.public_url, d.blocks, d.markdown, d.ink_uncovered_ratio,
               (d.markdown_verified_at IS NOT NULL)
        ORDER BY d.ink_uncovered_ratio DESC
        {'LIMIT $lim' if limit else ''}
        """,
        {**params, **({"lim": limit} if limit else {})},
    )
    return [{"file_path": r[0], "filename": r[1], "notice_type": r[2],
             "public_url": r[3], "blocks_json": r[4], "markdown": r[5],
             "old_ratio": r[6], "was_verified": r[7]} for r in rows]


def _bid() -> str:
    return f"blk_{secrets.token_hex(6)}"


def _column_band(x0: float) -> int:
    """Which third of the page a block starts in.

    Recovered blocks have to slot into the document's reading order. Sorting
    everything by y alone interleaves the columns of a multi-column notice and
    would scramble which lot each detail belongs to, so order is column-major:
    left column top-to-bottom, then centre, then right.
    """
    return min(2, max(0, int(x0 * 3)))


def _expand(bbox: list[float], margin: float) -> list[float]:
    x0, y0, x1, y1 = bbox
    return [max(0.0, x0 - margin), max(0.0, y0 - margin),
            min(1.0, x1 + margin), min(1.0, y1 + margin)]


def fix_one(t: dict, *, margin: float) -> dict:
    """Re-OCR one notice's missing patch. Never raises."""
    mode = datalab_mode_for(t["notice_type"])
    out = {**t, "mode": mode, "new_ratio": None, "new_chars": None,
           "recovered_blocks": 0, "note": "", "ok_to_write": False}
    for k in ("blocks_json", "markdown"):
        out.pop(k, None)
    src: Path | None = None
    try:
        blocks = json.loads(t["blocks_json"])
        blocks = blocks.get("blocks") if isinstance(blocks, dict) else blocks
        img = requests.get(t["public_url"], timeout=120).content

        before = score_ink_coverage(img, blocks)
        patch = (before.get("details") or {}).get("patch_bbox")
        if not patch or not before.get("flag"):
            out["note"] = "no patch to fix (re-measured clean)"
            return out
        region = {"bbox": _expand(patch, margin)}

        crop = _image_crop_to_png(img, region["bbox"])
        fd, name = tempfile.mkstemp(suffix=".png", prefix="patchfix_")
        with os.fdopen(fd, "wb") as f:
            f.write(crop)
        src = Path(name)

        result = datalab_api.run_file(src, output_format="json", mode=mode,
                                      timeout_s=900)
        _md, doc, _imgs = datalab_api.extract_payload(result)
        recovered = parse_datalab_blocks(doc)
        recovered = [b for b in recovered if (b.get("text") or "").strip()]
        if not recovered:
            out["note"] = "crop returned no text"
            return out
        page = int(blocks[0].get("page") or 1) if blocks else 1
        recovered = _merge_region_blocks([(region, recovered)], page=page)
        for b in recovered:
            b["id"] = _bid()
            b["source"] = "datalab-patchfix"

        combined = list(blocks) + recovered
        combined.sort(key=lambda b: (int(b.get("page") or 1),
                                     _column_band(b["bbox"][0]), b["bbox"][1]))
        for i, b in enumerate(combined):
            b["reading_order"] = i
            if not b.get("id"):
                b["id"] = _bid()

        markdown = assemble_markdown(combined)
        after = score_ink_coverage(img, combined)
        health = score_ocr_health(markdown, region=after)
        old_len = len(t["markdown"] or "")

        out.update(new_ratio=after["uncovered_ratio"], new_chars=len(markdown),
                   recovered_blocks=len(recovered), blocks=combined,
                   markdown=markdown, new_score=health["score"],
                   new_flags=health["flags"], old_chars=old_len)

        gain = (t["old_ratio"] or 0) - (after["uncovered_ratio"] or 0)
        if gain < MIN_GAIN:
            out["note"] = f"no coverage gain ({gain:+.1%}) — left alone"
        elif len(markdown) < old_len * MIN_KEEP_RATIO:
            out["note"] = (f"new markdown shorter ({len(markdown)} < "
                           f"{old_len}) — left alone")
        else:
            out["ok_to_write"] = True
    except Exception as e:
        out["note"] = f"{type(e).__name__}: {e}"
    finally:
        if src is not None:
            try:
                src.unlink()
            except FileNotFoundError:
                pass
    return out


def write_back(results: list[dict]) -> int:
    rows = [{
        "file_path": r["file_path"],
        "markdown": r["markdown"],
        "blocks_json": json.dumps({"schema_version": 1, "blocks": r["blocks"]},
                                  ensure_ascii=False),
        "ratio": r["new_ratio"], "score": r["new_score"], "flags": r["new_flags"],
    } for r in results if r.get("ok_to_write")]
    if not rows:
        return 0
    nq(
        """
        UNWIND $rows AS row
        MATCH (d:Document {file_path: row.file_path})
        SET d.markdown             = row.markdown,
            d.markdown_model       = 'datalab-patchfix',
            d.markdown_loaded_at   = datetime(),
            d.blocks               = row.blocks_json,
            d.blocks_revision      = coalesce(d.blocks_revision, 0) + 1,
            d.ink_uncovered_ratio  = row.ratio,
            d.ink_coverage_at      = datetime(),
            d.ocr_health_score     = row.score,
            d.ocr_health_flags     = row.flags,
            d.ocr_health_at        = datetime(),
            // The text a human approved has been replaced; ask again.
            d.markdown_verified_at = NULL,
            d.markdown_quality     = NULL,
            // langextract read the old text. Keep its output (stale beats
            // absent) but mark it for a re-run.
            d.extraction_stale_at  = datetime()
        """,
        {"rows": rows},
    )
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--upcoming", action="store_true",
                    help="only notices linked to an auction on/after today")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="select + preview only")
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN)
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    if not os.environ.get("DATALAB_API_KEY") and not args.dry_run:
        print("DATALAB_API_KEY not set")
        return 1

    targets = select_targets(upcoming=args.upcoming, limit=args.limit)
    verified = sum(1 for t in targets if t["was_verified"])
    print(f"Selected {len(targets)} missing-region Document(s)"
          f"  ({verified} carry a human sign-off that will be cleared)")
    if args.dry_run or not targets:
        for t in targets[:25]:
            print(f"  unread {t['old_ratio']:6.1%}  {t['notice_type']:<7} "
                  f"{'verified' if t['was_verified'] else '        '}  "
                  f"{t['filename'][:48]}")
        if args.dry_run:
            print("[dry-run] nothing written")
        return 0

    results: list[dict] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futs = {pool.submit(fix_one, t, margin=args.margin): t for t in targets}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            if r["ok_to_write"]:
                print(f"  [{i}/{len(targets)}] FIX   unread "
                      f"{r['old_ratio']:5.1%}->{r['new_ratio']:5.1%}  "
                      f"+{r['recovered_blocks']} blocks  chars "
                      f"{r['old_chars']}->{r['new_chars']}  {r['filename'][:34]}")
            else:
                print(f"  [{i}/{len(targets)}] skip  {r['note'][:52]}  "
                      f"{r['filename'][:34]}")

    wrote = write_back(results)
    fixed = [r for r in results if r.get("ok_to_write")]
    print(f"\nFixed {wrote}/{len(targets)} in {(time.time()-t0)/60:.1f} min")
    if fixed:
        gains = sorted((r["old_ratio"] - r["new_ratio"]) for r in fixed)
        print(f"coverage recovered — min {gains[0]:.1%}  "
              f"median {gains[len(gains)//2]:.1%}  max {gains[-1]:.1%}")
        print("\nRe-extract these (markdown changed):")
        for r in fixed:
            print(f"  {r['file_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
