"""
scripts/score_ink_coverage.py
-----------------------------
Flag notices whose parser dropped a region, by measuring unread ink.

``pipeline/ocr_health.py`` reads only the markdown we already have, so a notice
that lost a whole column still scores 100 with no flags — the case that started
this: SBI17861055659662.png, 29 blocks, health 100, 38% of its ink covered by no
block at all. This script downloads each notice's source image, runs
``pipeline.ink_coverage.score_ink_coverage`` against the stored blocks, and
merges the ``missing-region`` verdict into the document's OCR health.

Writes (only when a document is actually scored):
    d.ink_uncovered_ratio   float 0–1
    d.ink_coverage_at       datetime
    d.ocr_health_score      re-scored, including the missing-region penalty
    d.ocr_health_flags      existing text flags + missing-region

Health is recomputed from the stored markdown rather than patched, so a document
that no longer trips a text flag loses it here too — the score always reflects
one pass of the current rules, never an accumulation of old verdicts.

Runs entirely over HTTPS (Neo4j HTTP Query API + R2 source), so it works from
Bolt-firewalled environments — same pattern as scripts/reocr_low_health_datalab.py.

Usage:
    python -m scripts.score_ink_coverage --dry-run            # select + preview
    python -m scripts.score_ink_coverage --limit 20           # score 20, write
    python -m scripts.score_ink_coverage --all                # every doc with blocks
Options: --since 2026-08-01  --concurrency 6  --only-unscored

Auth: NEO4J_URI/USERNAME/PASSWORD(/DATABASE) in the environment.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from pipeline.ink_coverage import MISSING_REGION_MIN_RATIO, score_ink_coverage
from pipeline.ocr_health import score_ocr_health


# Blocks are stored as a JSON blob on the Document; pulling markdown too keeps
# the re-score honest but makes rows heavy, so batches stay modest.
DEFAULT_LIMIT = 50


# ── Neo4j over HTTPS (Query API v2) ─────────────────────────────────────────

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
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)["data"]["values"]
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


# ── selection ───────────────────────────────────────────────────────────────

def select_targets(*, since_iso: str | None, limit: int | None,
                   only_unscored: bool) -> list[dict]:
    """Documents that have both blocks and a fetchable image.

    PDFs are excluded: ink coverage needs a raster page, and a PDF would have to
    be rendered per page first (see pipeline/ink_coverage.py's scope note).
    """
    where = ["d.blocks IS NOT NULL", "d.blocks <> ''",
             "d.public_url IS NOT NULL", "d.public_url <> ''",
             "toLower(d.public_url) =~ '.*\\\\.(png|jpg|jpeg|webp)$'"]
    params: dict = {}
    if only_unscored:
        where.append("d.ink_uncovered_ratio IS NULL")
    if since_iso:
        match = "MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)"
        where.append("a.auction_start_dt >= datetime($t)")
        params["t"] = since_iso
    else:
        match = "MATCH (d:Document)"
    rows = nq(
        f"""
        {match}
        WHERE {' AND '.join(where)}
        WITH DISTINCT d
        RETURN d.file_path, d.filename, d.public_url, d.blocks, d.markdown,
               d.ocr_health_score
        ORDER BY d.filename
        {'LIMIT $lim' if limit else ''}
        """,
        {**params, **({"lim": limit} if limit else {})},
    )
    return [{"file_path": r[0], "filename": r[1], "public_url": r[2],
             "blocks_json": r[3], "markdown": r[4], "old_score": r[5]}
            for r in rows]


# ── scoring ─────────────────────────────────────────────────────────────────

def score_one(t: dict) -> dict:
    """Measure one notice. Never raises — a failure is reported, not fatal."""
    out = {**t, "ratio": None, "flags": [], "new_score": None, "note": ""}
    out.pop("blocks_json", None)
    out.pop("markdown", None)
    try:
        blocks = json.loads(t["blocks_json"])
        blocks = blocks.get("blocks") if isinstance(blocks, dict) else blocks
        r = requests.get(t["public_url"], timeout=120)
        r.raise_for_status()
        region = score_ink_coverage(r.content, blocks)
        if region["uncovered_ratio"] is None:
            out["note"] = str(region["details"].get("skipped") or "unscorable")
            return out
        health = score_ocr_health(t.get("markdown"), region=region)
        out["ratio"] = region["uncovered_ratio"]
        out["flags"] = health["flags"]
        out["new_score"] = health["score"]
        out["worst"] = region["details"].get("worst_column", {})
        out["ok_to_write"] = health["score"] is not None
    except Exception as e:
        out["note"] = f"{type(e).__name__}: {e}"
    return out


def write_back(results: list[dict]) -> int:
    rows = [{"file_path": r["file_path"], "ratio": r["ratio"],
             "score": r["new_score"], "flags": r["flags"]}
            for r in results if r.get("ok_to_write")]
    if not rows:
        return 0
    nq(
        """
        UNWIND $rows AS row
        MATCH (d:Document {file_path: row.file_path})
        SET d.ink_uncovered_ratio = row.ratio,
            d.ink_coverage_at     = datetime(),
            d.ocr_health_score    = row.score,
            d.ocr_health_flags    = row.flags,
            d.ocr_health_at       = datetime()
        """,
        {"rows": rows},
    )
    return len(rows)


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default=None,
                    help="restrict to notices whose auction is on/after this date")
    ap.add_argument("--all", action="store_true",
                    help="score every eligible Document (no --limit cap)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--only-unscored", action="store_true",
                    help="skip Documents that already carry ink_uncovered_ratio")
    ap.add_argument("--dry-run", action="store_true",
                    help="measure and print, write nothing")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    since_iso = f"{args.since}T00:00:00Z" if args.since else None
    targets = select_targets(since_iso=since_iso,
                             limit=None if args.all else args.limit,
                             only_unscored=args.only_unscored)
    print(f"Selected {len(targets)} Document(s) with blocks + a raster source "
          f"(threshold {MISSING_REGION_MIN_RATIO:.0%} unread ink)")
    if not targets:
        return 0

    results: list[dict] = []
    flagged = 0
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futs = {pool.submit(score_one, t): t for t in targets}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            if r["ratio"] is None:
                print(f"  [{i}/{len(targets)}] skip ({r['note']})  {r['filename'][:50]}")
                continue
            hit = "missing-region" in r["flags"]
            flagged += hit
            worst = r.get("worst") or {}
            print(f"  [{i}/{len(targets)}] unread {r['ratio']:6.1%}  "
                  f"health {str(r['old_score']):>3}->{str(r['new_score']):>4}  "
                  f"{'FLAG' if hit else '  ok'}  "
                  f"{(worst.get('where') or '') if hit else '':<6} "
                  f"{r['filename'][:44]}")

    scored = [r for r in results if r["ratio"] is not None]
    print(f"\nScored {len(scored)}/{len(targets)}   flagged {flagged}")
    if scored:
        ratios = sorted(r["ratio"] for r in scored)
        print(f"unread ink — min {ratios[0]:.1%}  median "
              f"{ratios[len(ratios)//2]:.1%}  max {ratios[-1]:.1%}")
    if args.dry_run:
        print("[dry-run] nothing written")
        return 0
    print(f"Wrote {write_back(results)} Document(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
