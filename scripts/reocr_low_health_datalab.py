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

def select_targets(health_below: int, *, since_iso: str | None = None,
                   notice_type: str | None = None) -> list[dict]:
    """Documents with ocr_health_score < threshold and a fetchable public_url.

    ``since_iso`` (a datetime) restricts to notices linked to an auction on/after
    that cutoff; pass ``None`` to select across all dates (and notices with no
    linked auction). ``notice_type`` ('single'/'multi') narrows to one tier;
    ``None`` or 'all' selects both.
    """
    where = ["d.ocr_health_score < $h",
             "d.public_url IS NOT NULL", "d.public_url <> ''"]
    params: dict = {"h": health_below}
    if since_iso:
        match = "MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)"
        where.append("a.auction_start_dt >= datetime($t)")
        params["t"] = since_iso
    else:
        match = "MATCH (d:Document)"
    if notice_type and notice_type != "all":
        where.append("coalesce(d.notice_type,'unknown') = $nt")
        params["nt"] = notice_type
    rows = nq(
        f"""
        {match}
        WHERE {' AND '.join(where)}
        WITH DISTINCT d
        RETURN d.file_path, d.filename, coalesce(d.notice_type,'unknown'),
               d.public_url, d.ocr_health_score
        ORDER BY d.ocr_health_score ASC
        """,
        params,
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
        out["parse_quality"] = datalab_api.parse_quality(result)
        if not markdown.strip() or health["score"] is None:
            out["note"] = "empty result — skipped"
        elif t["old_score"] is not None and health["score"] <= t["old_score"]:
            # Strict improvement only — equal-score docs (e.g. a dense multi-lot
            # notice Datalab also reads as one collapsed table) must NOT be
            # rewritten, or every re-run reprocesses them forever.
            out["note"] = f"no gain ({health['score']}<={t['old_score']}) — skipped"
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
            # Datalab's own 0–5 parse verdict; None on a cache-hit replay, and
            # then coalesce below keeps whatever the Document already had.
            "parse_quality": r.get("parse_quality"),
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
            d.ocr_health_at      = datetime(),
            d.parse_quality_score = coalesce(row.parse_quality,
                                             d.parse_quality_score),
            d.parse_quality_at    = CASE WHEN row.parse_quality IS NULL
                                        THEN d.parse_quality_at ELSE datetime() END
        """,
        {"rows": rows},
    )
    return len(rows)


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--health-below", type=int, default=70,
                    help="select docs with ocr_health_score strictly below this "
                         "(e.g. 91 covers <=90)")
    ap.add_argument("--since", default=None,
                    help="auction_start_dt cutoff (YYYY-MM-DD); default = today (UTC)")
    ap.add_argument("--all-dates", action="store_true",
                    help="ignore the auction-date cutoff; select across all dates "
                         "(and notices with no linked auction)")
    ap.add_argument("--notice-type", choices=["single", "multi", "all"],
                    default="all", help="restrict to one tier (cost staging)")
    ap.add_argument("--dry-run", action="store_true", help="select + preview only")
    ap.add_argument("--pilot", action="store_true",
                    help="re-OCR ~5 mixed docs (3 single + 2 multi) and write improved")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    if not os.environ.get("DATALAB_API_KEY") and not args.dry_run:
        return int(bool(print("DATALAB_API_KEY not set")))

    if args.all_dates:
        since_iso = None
        date_note = "all-dates"
    else:
        since = args.since or _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        since_iso = f"{since}T00:00:00Z"
        date_note = f"auction>={since}"

    targets = select_targets(args.health_below, since_iso=since_iso,
                             notice_type=args.notice_type)
    by_type: dict[str, int] = {}
    for t in targets:
        by_type[t["notice_type"]] = by_type.get(t["notice_type"], 0) + 1
    print(f"Target: {len(targets)} Documents  health<{args.health_below}  "
          f"{date_note}  notice_type={args.notice_type}  by_type={by_type}")

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
    # Flush writes in small batches as docs complete, so a crash mid-run (a long
    # accurate-tier pass can run 30+ min) loses at most FLUSH_EVERY docs' writes,
    # not the whole batch. Everything not yet flushed is simply re-selectable.
    FLUSH_EVERY = 10
    results: list[dict] = []
    pending: list[dict] = []
    wrote = 0
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futs = {pool.submit(reocr_one, t): t for t in targets}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            pending.append(r)
            verdict = ("WRITE" if r.get("ok_to_write")
                       else (r["note"] or "skip"))
            print(f"  [{i}/{len(targets)}] {r['notice_type']:<6} {r['mode']:<8} "
                  f"health {str(r['old_score']):>3}->{str(r['new_score']):>4}  "
                  f"{verdict}  {r['filename'][:44]}")
            if len(pending) >= FLUSH_EVERY:
                wrote += write_back(pending)
                pending = []
    if pending:
        wrote += write_back(pending)

    improved = sum(1 for r in results if r.get("ok_to_write"))
    print(f"\nDone. re-OCR'd={len(results)}  improved={improved}  written={wrote}  "
          f"skipped={len(results) - improved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
