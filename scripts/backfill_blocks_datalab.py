"""
scripts/backfill_blocks_datalab.py
----------------------------------
Give the block-less MinerU notices a block layer, without touching their text.

432 Documents carry MinerU markdown but no ``d.blocks``. That makes them
invisible to ink coverage (which compares ink against block boxes) and leaves
the annotator showing "backfill required" for every one of them.

This script re-OCRs each with Datalab and writes **only** the block layer plus
measurements. It deliberately never writes ``d.markdown``:

    langextract reads d.markdown and nothing else (see pipeline/extract_batch.py),
    and the review UI's highlight spans are character offsets into that exact
    string. Replacing the text would invalidate all 432 stored extractions and
    every highlight with them. Blocks are read only by the annotator, so writing
    them is additive — these Documents have none today.

Because of that split the measurements describe the **Datalab** parse, not the
stored MinerU text, and they are named accordingly (``shadow_*``). Two readings
come out of it:

  * ``shadow_ink_uncovered_ratio`` — did Datalab itself read the whole page?
  * ``shadow_char_gain`` — Datalab's text length / the stored markdown's. A
    notice where Datalab reads 3× more text is one whose stored markdown is
    missing content, which is the actual question for this cohort.

Nothing is promoted automatically. Deciding to adopt a Datalab parse means
rewriting that notice's markdown and re-extracting it — a per-notice call for a
human, on the much smaller set this run identifies.

Writes:
    d.blocks, d.blocks_revision (+1), d.blocks_source = 'datalab-backfill'
    d.parse_quality_score, d.parse_quality_at
    d.shadow_ink_uncovered_ratio, d.shadow_char_gain, d.shadow_chars, d.shadow_at

Usage:
    python -m scripts.backfill_blocks_datalab --dry-run          # select + preview
    python -m scripts.backfill_blocks_datalab --notice-type single
    python -m scripts.backfill_blocks_datalab --limit 20
Options: --concurrency 8  --flush-every 10

Auth: NEO4J_URI/USERNAME/PASSWORD(/DATABASE) + DATALAB_API_KEY.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from pipeline import datalab_api
from pipeline.config import datalab_mode_for
from pipeline.datalab import parse_datalab_blocks
from pipeline.ink_coverage import score_ink_coverage
from pipeline.mineru import assemble_markdown
from scripts.score_ink_coverage import nq


def select_targets(notice_type: str, limit: int | None) -> list[dict]:
    """MinerU Documents with text, a raster source, and no block layer."""
    where = ["(d.blocks IS NULL OR d.blocks = '')",
             "d.markdown IS NOT NULL", "d.markdown <> ''",
             "d.public_url IS NOT NULL",
             "toLower(d.public_url) =~ '.*\\\\.(png|jpg|jpeg|webp)$'"]
    params: dict = {}
    if notice_type != "all":
        where.append("coalesce(d.notice_type,'unknown') = $nt")
        params["nt"] = notice_type
    rows = nq(
        f"""
        MATCH (d:Document)
        WHERE {' AND '.join(where)}
        RETURN d.file_path, d.filename, coalesce(d.notice_type,'unknown'),
               d.public_url, size(d.markdown)
        ORDER BY d.filename
        {'LIMIT $lim' if limit else ''}
        """,
        {**params, **({"lim": limit} if limit else {})},
    )
    return [{"file_path": r[0], "filename": r[1], "notice_type": r[2],
             "public_url": r[3], "stored_chars": r[4]} for r in rows]


def _bid() -> str:
    return f"blk_{secrets.token_hex(6)}"


def backfill_one(t: dict) -> dict:
    """Re-OCR one notice for its blocks. Never raises."""
    mode = datalab_mode_for(t["notice_type"])
    out = {**t, "mode": mode, "blocks": None, "pq": None, "ratio": None,
           "gain": None, "chars": None, "note": ""}
    src: Path | None = None
    try:
        r = requests.get(t["public_url"], timeout=120)
        r.raise_for_status()
        img = r.content
        fd, name = tempfile.mkstemp(suffix=Path(t["filename"]).suffix or ".png")
        with os.fdopen(fd, "wb") as f:
            f.write(img)
        src = Path(name)

        result = datalab_api.run_file(src, output_format="json", mode=mode,
                                      timeout_s=900)
        _md, doc, _img = datalab_api.extract_payload(result)
        blocks = parse_datalab_blocks(doc)
        if not blocks:
            out["note"] = "no blocks returned"
            return out
        for b in blocks:
            b["id"] = _bid()

        text = result.get("markdown") or assemble_markdown(blocks)
        region = score_ink_coverage(img, blocks)
        out["blocks"] = blocks
        out["pq"] = datalab_api.parse_quality(result)
        out["ratio"] = region["uncovered_ratio"]
        out["chars"] = len(text or "")
        stored = t["stored_chars"] or 0
        out["gain"] = round(out["chars"] / stored, 3) if stored else None
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
    """Persist the block layer + shadow measurements. Never writes markdown."""
    rows = [{
        "file_path": r["file_path"],
        "blocks_json": json.dumps({"schema_version": 1, "blocks": r["blocks"]},
                                  ensure_ascii=False),
        "pq": r["pq"], "ratio": r["ratio"], "gain": r["gain"], "chars": r["chars"],
    } for r in results if r.get("ok_to_write")]
    if not rows:
        return 0
    nq(
        """
        UNWIND $rows AS row
        MATCH (d:Document {file_path: row.file_path})
        SET d.blocks                     = row.blocks_json,
            d.blocks_revision            = coalesce(d.blocks_revision, 0) + 1,
            d.blocks_source              = 'datalab-backfill',
            d.parse_quality_score        = coalesce(row.pq, d.parse_quality_score),
            d.parse_quality_at           = CASE WHEN row.pq IS NULL
                                              THEN d.parse_quality_at ELSE datetime() END,
            d.shadow_ink_uncovered_ratio = row.ratio,
            d.shadow_char_gain           = row.gain,
            d.shadow_chars               = row.chars,
            d.shadow_at                  = datetime()
        """,
        {"rows": rows},
    )
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--notice-type", choices=["single", "multi", "unknown", "all"],
                    default="all", help="restrict to one tier (cost staging)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="select + preview only")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--flush-every", type=int, default=10,
                    help="write completed docs in batches this size")
    args = ap.parse_args()

    if not os.environ.get("DATALAB_API_KEY") and not args.dry_run:
        print("DATALAB_API_KEY not set")
        return 1

    targets = select_targets(args.notice_type, args.limit)
    by_type: dict[str, int] = {}
    for t in targets:
        by_type[t["notice_type"]] = by_type.get(t["notice_type"], 0) + 1
    print(f"Selected {len(targets)} block-less Document(s)  by_type={by_type}")
    if args.dry_run or not targets:
        for t in targets[:20]:
            print(f"  {t['notice_type']:<7} {datalab_mode_for(t['notice_type']):<8} "
                  f"{t['stored_chars']:>6} chars  {t['filename'][:50]}")
        print("[dry-run] nothing written" if args.dry_run else "")
        return 0

    results: list[dict] = []
    pending: list[dict] = []
    wrote = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futs = {pool.submit(backfill_one, t): t for t in targets}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            pending.append(r)
            if r.get("ok_to_write"):
                unread = "  n/a" if r["ratio"] is None else f"{r['ratio']:.1%}"
                print(f"  [{i}/{len(targets)}] {r['mode']:<8} blocks {len(r['blocks']):>3}  "
                      f"pq {str(r['pq']):>5}  unread {unread:>6}  "
                      f"chars {r['stored_chars']}->{r['chars']} (x{r['gain']})  "
                      f"{r['filename'][:36]}")
            else:
                print(f"  [{i}/{len(targets)}] FAIL {r['note'][:60]}  {r['filename'][:36]}")
            if len(pending) >= args.flush_every:
                wrote += write_back(pending)
                pending = []
    if pending:
        wrote += write_back(pending)

    ok = [r for r in results if r.get("ok_to_write")]
    gains = sorted(r["gain"] for r in ok if r["gain"])
    print(f"\nBackfilled {wrote}/{len(targets)} in {(time.time()-t0)/60:.1f} min")
    if gains:
        big = [g for g in gains if g >= 1.5]
        print(f"text gain vs stored markdown — min x{gains[0]}  "
              f"median x{gains[len(gains)//2]}  max x{gains[-1]}")
        print(f"{len(big)} notice(s) where Datalab read >=1.5x the stored text "
              f"(candidates for promotion + re-extract)")
    print("markdown and extraction_json untouched — no re-extraction triggered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
