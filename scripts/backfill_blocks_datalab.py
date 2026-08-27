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

Two cohorts, selected with ``--cohort``:

``blockless`` (default)
    The original target set: MinerU notices carrying no ``d.blocks`` at all.
    Writing blocks is purely additive there — they have none — so this cohort
    writes the block layer *and* the shadow measurements.

``legacy-mineru``
    Notices still on MinerU markdown from before Datalab became the default
    engine (``--before``, default 2026-07-22) that have never been measured
    against Datalab. Almost all of these **already have a block layer**, and
    ~90 of them carry human re-extractions, so this cohort writes measurements
    ONLY — never blocks. See ``write_back``.

In both cohorts the markdown is left alone, for the reason above.

Writes:
    d.parse_quality_score, d.parse_quality_at
    d.shadow_ink_uncovered_ratio, d.shadow_char_gain, d.shadow_chars, d.shadow_at
    d.shadow_engine_mode
    (blockless cohort only, and only for a Document that has no block layer:)
    d.blocks, d.blocks_revision (+1), d.blocks_source = 'datalab-backfill'

Usage:
    python -m scripts.backfill_blocks_datalab --dry-run          # select + preview
    python -m scripts.backfill_blocks_datalab --notice-type single
    python -m scripts.backfill_blocks_datalab --limit 20

    # the legacy-MinerU shadow pass
    python -m scripts.backfill_blocks_datalab --cohort legacy-mineru --dry-run
    python -m scripts.backfill_blocks_datalab --cohort legacy-mineru --limit 25
Options: --concurrency 8  --flush-every 10  --before 2026-07-22

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


# Datalab became DESCRIPTION_OCR_ENGINE's default on this date; MinerU markdown
# loaded before it is legacy output nobody has compared against Datalab.
LEGACY_CUTOFF = "2026-07-22"


def select_targets(notice_type: str, limit: int | None, *,
                   cohort: str = "blockless",
                   before: str = LEGACY_CUTOFF) -> list[dict]:
    """Documents with text and a raster source, narrowed by ``cohort``.

    ``blockless``     — no block layer yet (the original target set).
    ``legacy-mineru`` — still on MinerU markdown loaded before ``before``, and
                        never measured against Datalab (no ``shadow_char_gain``).
                        Includes Documents that already have blocks; those keep
                        them (``write_back`` writes measurements only).
    """
    where = ["d.markdown IS NOT NULL", "d.markdown <> ''",
             "d.public_url IS NOT NULL",
             "toLower(d.public_url) =~ '.*\\\\.(png|jpg|jpeg|webp)$'"]
    params: dict = {}
    if cohort == "legacy-mineru":
        where += ["d.markdown_source = 'mineru'",
                  "d.markdown_loaded_at IS NOT NULL",
                  "date(d.markdown_loaded_at) < date($before)",
                  "d.shadow_char_gain IS NULL"]
        params["before"] = before
    else:
        where.append("(d.blocks IS NULL OR d.blocks = '')")
    if notice_type != "all":
        where.append("coalesce(d.notice_type,'unknown') = $nt")
        params["nt"] = notice_type
    rows = nq(
        f"""
        MATCH (d:Document)
        WHERE {' AND '.join(where)}
        RETURN d.file_path, d.filename, coalesce(d.notice_type,'unknown'),
               d.public_url, size(d.markdown),
               (d.blocks IS NOT NULL AND d.blocks <> ''),
               d.markdown_reextracted_at IS NOT NULL
        ORDER BY d.filename
        {'LIMIT $lim' if limit else ''}
        """,
        {**params, **({"lim": limit} if limit else {})},
    )
    return [{"file_path": r[0], "filename": r[1], "notice_type": r[2],
             "public_url": r[3], "stored_chars": r[4],
             "had_blocks": bool(r[5]), "human_edited": bool(r[6])} for r in rows]


def _bid() -> str:
    return f"blk_{secrets.token_hex(6)}"


def backfill_one(t: dict) -> dict:
    """Run one notice through Datalab for its blocks + measurements.

    Blocks are needed either way: even where they are not written back, ink
    coverage is scored against them. Never raises — a failure comes back as a
    row with ``note`` set and no ``ok_to_write``.
    """
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

        # skip_cache: on a cache hit Datalab replays a prior conversion without
        # re-scoring it, so parse_quality_score comes back null (see
        # datalab_api.parse_quality). The score is the point of a shadow pass.
        result = datalab_api.run_file(src, output_format="json", mode=mode,
                                      timeout_s=900,
                                      extra={"skip_cache": "true"})
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


def write_back(results: list[dict]) -> tuple[int, int]:
    """Persist shadow measurements, and blocks only where there are none.

    Returns ``(rows_measured, rows_given_blocks)``.

    A Document that already has a block layer keeps it. Its blocks may carry
    human re-extractions (``source: "human"`` blocks written through the
    annotator), and this pass is a measurement, not a verdict — overwriting
    them would discard review work to record a number. So the block write is
    split out and gated on ``had_blocks`` being false, which in the
    ``legacy-mineru`` cohort is essentially never.
    """
    ok = [r for r in results if r.get("ok_to_write")]
    if not ok:
        return 0, 0
    measured = [{
        "file_path": r["file_path"],
        "pq": r["pq"], "ratio": r["ratio"], "gain": r["gain"], "chars": r["chars"],
        "mode": r["mode"],
    } for r in ok]
    nq(
        """
        UNWIND $rows AS row
        MATCH (d:Document {file_path: row.file_path})
        SET d.parse_quality_score        = coalesce(row.pq, d.parse_quality_score),
            d.parse_quality_at           = CASE WHEN row.pq IS NULL
                                              THEN d.parse_quality_at ELSE datetime() END,
            d.shadow_ink_uncovered_ratio = row.ratio,
            d.shadow_char_gain           = row.gain,
            d.shadow_chars               = row.chars,
            d.shadow_engine_mode         = row.mode,
            d.shadow_at                  = datetime()
        """,
        {"rows": measured},
    )

    new_blocks = [{
        "file_path": r["file_path"],
        "blocks_json": json.dumps({"schema_version": 1, "blocks": r["blocks"]},
                                  ensure_ascii=False),
    } for r in ok if not r.get("had_blocks")]
    if new_blocks:
        nq(
            """
            UNWIND $rows AS row
            MATCH (d:Document {file_path: row.file_path})
            WHERE d.blocks IS NULL OR d.blocks = ''
            SET d.blocks          = row.blocks_json,
                d.blocks_revision = coalesce(d.blocks_revision, 0) + 1,
                d.blocks_source   = 'datalab-backfill'
            """,
            {"rows": new_blocks},
        )
    return len(measured), len(new_blocks)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cohort", choices=["blockless", "legacy-mineru"],
                    default="blockless",
                    help="blockless: Documents with no block layer (default). "
                         "legacy-mineru: pre-cutoff MinerU notices never "
                         "measured against Datalab (measurements only)")
    ap.add_argument("--before", default=LEGACY_CUTOFF,
                    help=f"legacy-mineru cutoff date (default {LEGACY_CUTOFF}, "
                         "when Datalab became the default engine)")
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

    targets = select_targets(args.notice_type, args.limit,
                             cohort=args.cohort, before=args.before)
    by_type: dict[str, int] = {}
    for t in targets:
        by_type[t["notice_type"]] = by_type.get(t["notice_type"], 0) + 1
    keeping = sum(1 for t in targets if t["had_blocks"])
    human = sum(1 for t in targets if t["human_edited"])
    label = ("block-less" if args.cohort == "blockless"
             else f"legacy-MinerU (pre-{args.before}, unmeasured)")
    print(f"Selected {len(targets)} {label} Document(s)  by_type={by_type}")
    print(f"  blocks kept as-is: {keeping}  (of which human-re-extracted: {human})"
          f"   blocks to write: {len(targets) - keeping}")
    if args.dry_run or not targets:
        for t in targets[:20]:
            print(f"  {t['notice_type']:<7} {datalab_mode_for(t['notice_type']):<8} "
                  f"{t['stored_chars']:>6} chars  "
                  f"{'keep-blocks' if t['had_blocks'] else 'write-blocks':<12} "
                  f"{t['filename'][:50]}")
        print("[dry-run] nothing written" if args.dry_run else "")
        return 0

    results: list[dict] = []
    pending: list[dict] = []
    wrote = 0
    wrote_blocks = 0
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
                m, b = write_back(pending)
                wrote += m
                wrote_blocks += b
                pending = []
    if pending:
        m, b = write_back(pending)
        wrote += m
        wrote_blocks += b

    ok = [r for r in results if r.get("ok_to_write")]
    gains = sorted(r["gain"] for r in ok if r["gain"])
    print(f"\nMeasured {wrote}/{len(targets)} in {(time.time()-t0)/60:.1f} min  "
          f"(block layers written: {wrote_blocks})")
    if gains:
        big = [r for r in ok if r["gain"] and r["gain"] >= 1.5]
        print(f"text gain vs stored markdown — min x{gains[0]}  "
              f"median x{gains[len(gains)//2]}  max x{gains[-1]}")
        print(f"{len(big)} notice(s) where Datalab read >=1.5x the stored text "
              f"(candidates for promotion + re-extract):")
        for r in sorted(big, key=lambda r: -r["gain"])[:20]:
            print(f"    x{r['gain']:<6} {r['stored_chars']:>6} -> {r['chars']:<6} "
                  f"{r['filename'][:50]}")
    print("markdown and extraction_json untouched — no re-extraction triggered")
    if wrote_blocks == 0:
        print("existing block layers left intact — human re-extractions preserved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
