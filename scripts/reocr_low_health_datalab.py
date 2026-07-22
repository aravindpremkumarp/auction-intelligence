"""
scripts/reocr_low_health_datalab.py
-----------------------------------
Targeted re-OCR of low-health, upcoming sale notices with Datalab.

Selects Documents whose ``ocr_health_score`` is below a threshold AND that are
linked to an auction on/after a cutoff date, re-OCRs each with Datalab (tier by
notice_type: single -> fast, multi -> accurate), and — only when the result is
at least as healthy as what's there now — writes back the fresh markdown, blocks
and health score.

Runs entirely over HTTPS (Neo4j HTTP Query API + R2 source + Datalab), so it
works from Bolt-firewalled environments — same pattern as
``scripts/auto_region_reingest.py``.

Safety:
  * ``--dry-run``            select + preview only; no OCR, no writes.
  * a doc is written only if the new health score is >= the old one and the new
    markdown is non-empty (a Datalab whiff never overwrites existing text).
  * writing bumps ``d.blocks_revision`` so the annotator reloads the new blocks.

Usage:
    python -m scripts.reocr_low_health_datalab --dry-run          # count + preview
    python -m scripts.reocr_low_health_datalab --pilot            # ~5 mixed docs, write improved
    python -m scripts.reocr_low_health_datalab                    # the full set
    python -m scripts.reocr_low_health_datalab --limit 30
Options: --health-below 70  --since 2026-07-22  --concurrency 4

Auth: NEO4J_URI/USERNAME/PASSWORD(/DATABASE) + DATALAB_API_KEY in the environment.
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import mimetypes
import os
import secrets
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from pipeline import datalab_api
from pipeline.config import datalab_mode_for
from pipeline.datalab import parse_datalab_blocks
from pipeline.mineru import assemble_markdown
from pipeline.ocr_health import score_ocr_health


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
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)["data"]["values"]
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


# ── selection ───────────────────────────────────────────────────────────────

def select_targets(since_iso: str, health_below: int) -> list[dict]:
    """Documents with ocr_health_score < threshold linked to an auction on/after
    ``since_iso`` (a datetime), that have a fetchable public_url."""
    rows = nq(
        """
        MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
        WHERE d.ocr_health_score < $h
          AND a.auction_start_dt >= datetime($t)
          AND d.public_url IS NOT NULL AND d.public_url <> ''
        WITH DISTINCT d
        RETURN d.file_path, d.filename, coalesce(d.notice_type,'unknown'),
               d.public_url, d.ocr_health_score
        ORDER BY d.ocr_health_score ASC
        """,
        {"h": health_below, "t": since_iso},
    )
    return [{"file_path": r[0], "filename": r[1], "notice_type": r[2],
             "public_url": r[3], "old_score": r[4]} for r in rows]


def pick_pilot(targets: list[dict], n_single: int = 3, n_multi: int = 2) -> list[dict]:
    singles = [t for t in targets if t["notice_type"] == "single"][:n_single]
    multis = [t for t in targets if t["notice_type"] == "multi"][:n_multi]
    return singles + multis


# ── source fetch + Datalab ──────────────────────────────────────────────────

def fetch_source(url: str) -> Path:
    r = requests.get(url, timeout=90)
    r.raise_for_status()
    base = Path(urllib.parse.urlparse(url).path).name or "notice"
    if not Path(base).suffix:
        ct = (r.headers.get("content-type") or "").split(";")[0].strip()
        base += mimetypes.guess_extension(ct) or ".pdf"
    fd, name = tempfile.mkstemp(suffix=Path(base).suffix or ".bin", prefix="reocr_")
    with os.fdopen(fd, "wb") as f:
        f.write(r.content)
    return Path(name)


def _bid() -> str:
    return f"blk_{secrets.token_hex(6)}"


def reocr_one(t: dict) -> dict:
    """Re-OCR one notice with Datalab. Returns a result dict; never raises."""
    mode = datalab_mode_for(t["notice_type"])
    out = {**t, "mode": mode, "new_score": None, "new_flags": [],
           "wrote": False, "note": ""}
    src: Path | None = None
    try:
        src = fetch_source(t["public_url"])
        result = datalab_api.run_file(src, output_format="json", mode=mode)
        _md, doc, _img = datalab_api.extract_payload(result)
        blocks = parse_datalab_blocks(doc)
        for b in blocks:
            b["id"] = _bid()
        markdown = result.get("markdown") or assemble_markdown(blocks)
        health = score_ocr_health(markdown)
        out["markdown"] = markdown
        out["blocks"] = blocks
        out["new_score"] = health["score"]
        out["new_flags"] = health["flags"]
        if not markdown.strip() or health["score"] is None:
            out["note"] = "empty result — skipped"
        elif t["old_score"] is not None and health["score"] < t["old_score"]:
            out["note"] = f"worse ({health['score']}<{t['old_score']}) — skipped"
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


# ── write-back ──────────────────────────────────────────────────────────────

def write_back(results: list[dict]) -> int:
    """Persist improved re-OCRs. Bumps blocks_revision so the annotator reloads."""
    rows = []
    for r in results:
        if not r.get("ok_to_write"):
            continue
        rows.append({
            "file_path":  r["file_path"],
            "markdown":   r["markdown"],
            "blocks_raw": json.dumps(r["blocks"], ensure_ascii=False),
            "blocks_json": json.dumps(
                {"schema_version": 1, "blocks": r["blocks"]}, ensure_ascii=False),
            "model":      f"datalab-{r['mode']}",
            "score":      r["new_score"],
            "flags":      r["new_flags"],
        })
    if not rows:
        return 0
    nq(
        """
        UNWIND $rows AS row
        MATCH (d:Document {file_path: row.file_path})
        SET d.markdown           = row.markdown,
            d.markdown_source    = 'datalab',
            d.markdown_model     = row.model,
            d.markdown_loaded_at = datetime(),
            d.markdown_raw       = row.markdown,
            d.markdown_raw_at    = datetime(),
            d.blocks_raw         = row.blocks_raw,
            d.blocks             = row.blocks_json,
            d.blocks_revision    = coalesce(d.blocks_revision, 0) + 1,
            d.ocr_health_score   = row.score,
            d.ocr_health_flags   = row.flags,
            d.ocr_health_at      = datetime()
        """,
        {"rows": rows},
    )
    return len(rows)


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--health-below", type=int, default=70)
    ap.add_argument("--since", default=None,
                    help="auction_start_dt cutoff (YYYY-MM-DD); default = today (UTC)")
    ap.add_argument("--dry-run", action="store_true", help="select + preview only")
    ap.add_argument("--pilot", action="store_true",
                    help="re-OCR ~5 mixed docs (3 single + 2 multi) and write improved")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    if not os.environ.get("DATALAB_API_KEY") and not args.dry_run:
        return int(bool(print("DATALAB_API_KEY not set")))

    since = args.since or _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    since_iso = f"{since}T00:00:00Z"

    targets = select_targets(since_iso, args.health_below)
    by_type: dict[str, int] = {}
    for t in targets:
        by_type[t["notice_type"]] = by_type.get(t["notice_type"], 0) + 1
    print(f"Target: {len(targets)} Documents  health<{args.health_below}  "
          f"auction_start_dt>={since}  by_type={by_type}")

    if args.pilot:
        targets = pick_pilot(targets)
    elif args.limit:
        targets = targets[:args.limit]

    if args.dry_run:
        print(f"\n[dry-run] would re-OCR {len(targets)}:")
        for t in targets[:50]:
            print(f"  {t['old_score']:>3}  {t['notice_type']:<7}  "
                  f"{datalab_mode_for(t['notice_type']):<8}  {t['filename']}")
        return 0

    print(f"\nRe-OCR {len(targets)} notice(s) with Datalab "
          f"(concurrency={args.concurrency})…")
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futs = {pool.submit(reocr_one, t): t for t in targets}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            verdict = ("WRITE" if r.get("ok_to_write")
                       else (r["note"] or "skip"))
            print(f"  [{i}/{len(targets)}] {r['notice_type']:<6} {r['mode']:<8} "
                  f"health {str(r['old_score']):>3}->{str(r['new_score']):>4}  "
                  f"{verdict}  {r['filename'][:44]}")

    wrote = write_back(results)
    improved = sum(1 for r in results if r.get("ok_to_write"))
    print(f"\nDone. re-OCR'd={len(results)}  improved={improved}  written={wrote}  "
          f"skipped={len(results) - improved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
