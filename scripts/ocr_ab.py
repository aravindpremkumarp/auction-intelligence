"""OCR A/B: run MinerU and Datalab on the same notices, side-by-side.

For each notice, produces both engines' markdown + canonical block list, a
metrics row (chars, blocks, tables, images, wall-clock latency, and Datalab's
quality score + cost when present), and a self-contained HTML report you can
open in a browser to eyeball the two outputs next to each other.

Isolation: results land under ``pipeline/cache/ab_ocr/`` and nothing writes to
Neo4j or the production MinerU caches — an A/B run can't disturb live data.
The MinerU side *reads* the production cache when a notice was already OCR'd
(free, no API call); only cache misses spend a MinerU call.

Pick inputs one of several ways:

  --files A B C ...     explicit notice paths
  --urls U1 U2 ...      notice URLs to download and A/B
  --urls-file PATH      file of notice URLs (one per line or comma-sep) — the
                        path CI uses (public R2/web URLs, only the OCR keys)
  --dir PATH            every supported file directly under PATH
  --from-worklist       Documents from Neo4j (reuses fetch_all_work);
                        --missing-only restricts to un-OCR'd ones

Common flags:
  --limit N             cap the number of files
  --concurrency N       files processed in parallel (default 4)
  --mode M              Datalab tier: fast (default) | balanced | accurate
  --native-markdown     also fetch Datalab's own markdown (a 2nd call/file);
                        default derives markdown from the parsed block list
  --datalab-only        skip MinerU (Datalab-only pass)
  --mineru-only         skip Datalab (MinerU-only pass)

Examples:
  python -m scripts.ocr_ab --dir downloads/live_properties --limit 20
  python -m scripts.ocr_ab --from-worklist --missing-only --limit 30
  python -m scripts.ocr_ab --files downloads/live_properties/notice1.pdf

Auth: DATALAB_API_KEY + MINERU_API_KEY in .env.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

from pipeline.config import PIPELINE_DIR
from pipeline import datalab as DL
from pipeline import datalab_api as DLA
from pipeline.mineru import (
    MINERU_SUPPORTED_EXTS,
    assemble_markdown,
    cached_blocks_for_file_path,
    cached_markdown_for_file_path,
    find_disk_path,
    parse_mineru_content_list,
    preclean_if_needed,
    safe_cache_name,
)


load_dotenv()

AB_DIR         = PIPELINE_DIR / "cache" / "ab_ocr"
AB_MINERU_DIR  = AB_DIR / "mineru"
AB_DATALAB_DIR = AB_DIR / "datalab"
AB_INPUTS_DIR  = AB_DIR / "inputs"   # notices downloaded from --urls / --urls-file


# ── file selection ───────────────────────────────────────────────────────────

def _item(disk: Path, file_path: str | None = None) -> dict:
    """Normalize one input into the {filename, file_path, disk_path} shape the
    engines expect. ``file_path`` is the stable cache key (defaults to the disk
    path for --files/--dir; the Document.file_path for --from-worklist)."""
    return {"filename": disk.name,
            "file_path": file_path or str(disk),
            "disk_path": disk}


def _read_url_list(args) -> list[str]:
    """Gather URLs from --urls and/or --urls-file, de-duped in order.

    URLs may be separated by any whitespace (newlines OR spaces) or commas —
    GitHub's workflow_dispatch inputs are single-line, so a pasted "one per
    line" list actually arrives space-joined; splitting on whitespace handles
    every paste shape. Comment lines (starting with #) and blanks are dropped.
    """
    urls: list[str] = list(args.urls or [])
    if args.urls_file:
        for line in Path(args.urls_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.extend(tok for tok in re.split(r"[\s,]+", line) if tok)
    seen: set[str] = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


def _download_url(url: str, idx: int) -> dict | None:
    """Download one notice URL into AB_INPUTS_DIR; return its input item.

    The cache/report key stays the URL (stable identity); the on-disk name is
    prefixed with ``idx`` so distinct URLs sharing a basename don't collide.
    A download failure is logged and skipped (returns None) so one bad URL
    can't abort the run. The A/B needs the bytes locally for both engines
    (MinerU uploads them; Datalab could take a file_url but MinerU can't).
    """
    import mimetypes
    import urllib.parse

    import requests

    AB_INPUTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        r = requests.get(url, timeout=120)
        r.raise_for_status()
    except Exception as e:
        print(f"  [skip] download failed: {url} ({type(e).__name__}: {e})")
        return None
    base = Path(urllib.parse.urlparse(url).path).name or "notice"
    if not Path(base).suffix:
        ct = (r.headers.get("content-type") or "").split(";")[0].strip()
        base += mimetypes.guess_extension(ct) or ".pdf"
    disk = AB_INPUTS_DIR / f"{idx:03d}_{base}"
    disk.write_bytes(r.content)
    return _item(disk, file_path=url)


def collect_files(args) -> list[dict]:
    items: list[dict] = []
    if args.files:
        for p in args.files:
            disk = Path(p)
            if disk.exists():
                items.append(_item(disk))
            else:
                print(f"  [skip] not found: {p}")
    elif args.urls or args.urls_file:
        for i, url in enumerate(_read_url_list(args)):
            it = _download_url(url, i)
            if it is not None:
                items.append(it)
    elif args.dir:
        root = Path(args.dir)
        for disk in sorted(root.iterdir()) if root.exists() else []:
            if disk.is_file() and disk.suffix.lower() in MINERU_SUPPORTED_EXTS:
                items.append(_item(disk))
    elif args.from_worklist:
        # Lazy import: only --from-worklist needs Neo4j.
        from scripts.ocr_with_mineru import fetch_all_work
        for w in fetch_all_work(missing_only=args.missing_only):
            disk = find_disk_path(w["filename"])
            if disk is not None and disk.suffix.lower() in MINERU_SUPPORTED_EXTS:
                items.append(_item(disk, file_path=w["file_path"]))
    else:
        raise SystemExit("pick an input: --files, --urls, --urls-file, --dir, or --from-worklist")

    # De-dupe on cache key, keep order, apply --limit.
    seen: set[str] = set()
    unique = [it for it in items
              if not (it["file_path"] in seen or seen.add(it["file_path"]))]
    if args.limit:
        unique = unique[:args.limit]
    return unique


# ── engines ──────────────────────────────────────────────────────────────────

def _metrics(blocks: list[dict], markdown: str) -> dict:
    return {
        "chars":  len(markdown or ""),
        "blocks": len(blocks),
        "tables": sum(1 for b in blocks if b.get("label") == "Table"),
        "images": sum(1 for b in blocks if b.get("label") == "Image"),
        "titles": sum(1 for b in blocks if b.get("label") == "Title"),
    }


def run_mineru(item: dict) -> dict:
    """MinerU markdown + canonical blocks for one notice, isolated from prod.

    Reuses the production markdown/blocks cache on a hit (no API call); on a
    miss, calls MinerU via the raw helpers and parses the zip in memory —
    deliberately NOT ``download_and_cache`` (which writes the prod cache and can
    archive to R2). Returns ``{ok, source, markdown, blocks, latency, error}``.
    """
    fp = item["file_path"]
    # Cache hit: reuse what the live pipeline already produced.
    cached_md     = cached_markdown_for_file_path(fp)
    cached_blocks = cached_blocks_for_file_path(fp)
    if cached_md is not None and cached_blocks is not None:
        return {"ok": True, "source": "cache", "latency": 0.0,
                "markdown": cached_md,
                "blocks": parse_mineru_content_list(cached_blocks)}

    if not os.environ.get("MINERU_API_KEY"):
        return {"ok": False, "error": "MINERU_API_KEY not set (and no cache hit)"}

    # Lazy import so a Datalab-only run doesn't pull the MinerU HTTP client.
    from pipeline.mineru_api import (
        download_zip, parse_zip_payload, poll as mineru_poll,
        request_batch, upload_files,
    )

    t0 = time.monotonic()
    send_disk, was_pre = preclean_if_needed(item["disk_path"])
    call_item = {
        "filename": (Path(item["filename"]).stem + "_preclean.jpg") if was_pre
                    else item["filename"],
        "file_path": fp,
        "disk_path": send_disk,
    }
    try:
        batch_id, urls = request_batch([call_item])
        upload_files([call_item], urls)
        rows = mineru_poll(batch_id)
        row = next((r for r in rows if r.get("state") == "done"
                    and r.get("full_zip_url")), None)
        if row is None:
            return {"ok": False, "error": "MinerU: no completed row"}
        zip_bytes = download_zip(row["full_zip_url"])
        if not zip_bytes:
            return {"ok": False, "error": "MinerU: zip download failed"}
        md_raw, blocks_raw = parse_zip_payload(zip_bytes)
        blocks = parse_mineru_content_list(blocks_raw or [])
        markdown = md_raw if md_raw is not None else assemble_markdown(blocks)
        return {"ok": True, "source": "api",
                "latency": time.monotonic() - t0,
                "markdown": markdown, "blocks": blocks}
    except Exception as e:  # one bad file must not kill the run
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        if was_pre:
            try:
                send_disk.unlink()
            except FileNotFoundError:
                pass


def run_datalab(item: dict, *, mode: str, native_markdown: bool) -> dict:
    """Datalab markdown + canonical blocks for one notice.

    One ``output_format=json`` call gives blocks (with bboxes); markdown is
    derived from those blocks by default, or fetched natively with a second
    ``output_format=markdown`` call when ``native_markdown`` is set.
    """
    if not os.environ.get("DATALAB_API_KEY"):
        return {"ok": False, "error": "DATALAB_API_KEY not set"}
    t0 = time.monotonic()
    try:
        result = DLA.run_file(item["disk_path"], output_format="json", mode=mode)
        _md, doc_json, _images = DLA.extract_payload(result)
        blocks = DL.parse_datalab_blocks(doc_json)
        if native_markdown:
            md_result = DLA.run_file(item["disk_path"],
                                     output_format="markdown", mode=mode)
            markdown = DLA.extract_payload(md_result)[0] or assemble_markdown(blocks)
        else:
            # Some payloads carry markdown alongside json; prefer it, else derive.
            markdown = result.get("markdown") or assemble_markdown(blocks)
        return {"ok": True, "source": "api",
                "latency": time.monotonic() - t0,
                "markdown": markdown, "blocks": blocks,
                "quality": result.get("parse_quality_score"),
                "cost": result.get("cost_breakdown"),
                "page_count": result.get("page_count")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _write_engine_cache(engine_dir: Path, file_path: str, res: dict) -> None:
    if not res.get("ok"):
        return
    engine_dir.mkdir(parents=True, exist_ok=True)
    safe = safe_cache_name(file_path)
    (engine_dir / f"{safe}.md").write_text(res.get("markdown") or "", encoding="utf-8")
    (engine_dir / f"{safe}.blocks.json").write_text(
        json.dumps(res.get("blocks") or [], ensure_ascii=False, indent=2),
        encoding="utf-8")


def process_one(item: dict, *, run_m: bool, run_d: bool,
                mode: str, native_markdown: bool) -> dict:
    m = run_mineru(item) if run_m else {"ok": False, "error": "skipped"}
    d = (run_datalab(item, mode=mode, native_markdown=native_markdown)
         if run_d else {"ok": False, "error": "skipped"})
    _write_engine_cache(AB_MINERU_DIR, item["file_path"], m)
    _write_engine_cache(AB_DATALAB_DIR, item["file_path"], d)
    return {
        "filename": item["filename"],
        "file_path": item["file_path"],
        "mineru": {**m, "metrics": _metrics(m.get("blocks") or [], m.get("markdown") or "")} if m.get("ok") else m,
        "datalab": {**d, "metrics": _metrics(d.get("blocks") or [], d.get("markdown") or "")} if d.get("ok") else d,
    }


# ── report ───────────────────────────────────────────────────────────────────

def _cell(res: dict) -> str:
    if not res.get("ok"):
        return f'<div class="err">✗ {html.escape(str(res.get("error", "error")))}</div>'
    mt = res["metrics"]
    extra = ""
    if res.get("quality") is not None:
        extra = f' · quality {res["quality"]}'
    badge = (f'<div class="badge">{mt["chars"]} chars · {mt["blocks"]} blocks · '
             f'{mt["tables"]} tbl · {mt["images"]} img · '
             f'{res.get("latency", 0):.1f}s{extra}</div>')
    body = html.escape(res.get("markdown") or "")
    return f'{badge}<pre>{body}</pre>'


def write_report(rows: list[dict]) -> Path:
    AB_DIR.mkdir(parents=True, exist_ok=True)
    (AB_DIR / "report.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    parts = ["""<!doctype html><meta charset="utf-8">
<title>OCR A/B — MinerU vs Datalab</title>
<style>
 body{font:14px/1.5 system-ui,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
 h1{padding:16px 20px;margin:0;border-bottom:1px solid #222;font-size:18px}
 .doc{border-bottom:1px solid #222;padding:12px 20px}
 .doc h2{font-size:14px;margin:0 0 8px;color:#8ab4f8;font-weight:600}
 .cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
 .col h3{margin:0 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:#9aa0a6}
 .badge{font-size:12px;color:#9aa0a6;margin-bottom:6px}
 pre{white-space:pre-wrap;word-break:break-word;background:#171a21;border:1px solid #222;
     border-radius:6px;padding:10px;max-height:460px;overflow:auto;margin:0;font-size:12px}
 .err{color:#f28b82;background:#2a1416;border:1px solid #4a2023;border-radius:6px;padding:10px}
 @media (max-width:820px){.cols{grid-template-columns:1fr}}
</style>
<h1>OCR A/B — MinerU vs Datalab</h1>"""]
    for r in rows:
        parts.append(
            f'<div class="doc"><h2>{html.escape(r["filename"])}</h2><div class="cols">'
            f'<div class="col"><h3>MinerU</h3>{_cell(r["mineru"])}</div>'
            f'<div class="col"><h3>Datalab</h3>{_cell(r["datalab"])}</div>'
            f'</div></div>')
    report = AB_DIR / "report.html"
    report.write_text("\n".join(parts), encoding="utf-8")
    return report


def _avg(nums: list[float]) -> float:
    return sum(nums) / len(nums) if nums else 0.0


def print_summary(rows: list[dict]) -> None:
    for eng in ("mineru", "datalab"):
        oks = [r[eng] for r in rows if r[eng].get("ok")]
        errs = len(rows) - len(oks)
        chars = [o["metrics"]["chars"] for o in oks]
        tables = [o["metrics"]["tables"] for o in oks]
        lat = [o.get("latency", 0.0) for o in oks if o.get("source") != "cache"]
        print(f"\n  {eng:8} ok={len(oks)} err={errs}  "
              f"avg_chars={_avg(chars):.0f}  avg_tables={_avg(tables):.1f}  "
              f"avg_latency={_avg(lat):.1f}s")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--files", nargs="+", help="explicit notice paths")
    src.add_argument("--urls", nargs="+", help="notice URLs to download and A/B")
    src.add_argument("--urls-file",
                     help="file with notice URLs (one per line or comma-sep)")
    src.add_argument("--dir", help="directory of notices")
    src.add_argument("--from-worklist", action="store_true",
                     help="pull Documents from Neo4j")
    p.add_argument("--missing-only", action="store_true",
                   help="with --from-worklist: only un-OCR'd Documents")
    p.add_argument("--limit", type=int, default=None, help="cap file count")
    p.add_argument("--concurrency", type=int, default=4, help="parallel files")
    p.add_argument("--mode", default="fast",
                   choices=["fast", "balanced", "accurate"],
                   help="Datalab tier (default fast)")
    p.add_argument("--native-markdown", action="store_true",
                   help="also fetch Datalab's own markdown (2nd call/file)")
    p.add_argument("--datalab-only", action="store_true")
    p.add_argument("--mineru-only", action="store_true")
    args = p.parse_args()

    run_m = not args.datalab_only
    run_d = not args.mineru_only

    items = collect_files(args)
    print(f"A/B on {len(items)} notice(s)  "
          f"engines={'MinerU ' if run_m else ''}{'Datalab' if run_d else ''}  "
          f"datalab_mode={args.mode}")
    if not items:
        sys.exit("no notices collected — check the URL input (nothing downloaded)")

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futs = {
            pool.submit(process_one, it, run_m=run_m, run_d=run_d,
                        mode=args.mode, native_markdown=args.native_markdown): it
            for it in items
        }
        for i, fut in enumerate(as_completed(futs), 1):
            it = futs[fut]
            try:
                row = fut.result()
            except Exception as e:
                row = {"filename": it["filename"], "file_path": it["file_path"],
                       "mineru": {"ok": False, "error": str(e)},
                       "datalab": {"ok": False, "error": str(e)}}
            rows.append(row)
            m_ok = "✓" if row["mineru"].get("ok") else "✗"
            d_ok = "✓" if row["datalab"].get("ok") else "✗"
            print(f"  [{i}/{len(items)}] {row['filename'][:50]:50}  "
                  f"MinerU {m_ok}  Datalab {d_ok}")

    # Stable order for the report (as-submitted, not as-completed).
    order = {it["file_path"]: n for n, it in enumerate(items)}
    rows.sort(key=lambda r: order.get(r["file_path"], 0))

    report = write_report(rows)
    print_summary(rows)
    print(f"\n  report: {report}")
    print(f"  per-engine caches: {AB_MINERU_DIR}  |  {AB_DATALAB_DIR}")


if __name__ == "__main__":
    main()
