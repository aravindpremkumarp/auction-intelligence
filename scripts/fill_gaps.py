"""Fill the key facts stored extractions are missing, with short reads.

For each selected notice: list the lots missing a key fact (reserve price,
auction date, property type, location, extent, full description, possession),
read just that lot's text with a lean prompt asking only for those facts
(pipeline/gap_fill), fold the answers into the stored read — adding only what
a lot lacks — and save through the keep-better gate, so a notice is written
only when it gained something and lost nothing.

A notice whose text changed since its stored read is skipped: its old spans no
longer point into the text, so it needs a full re-read
(reset_langextract_and_extract --stale), not a fill.

Saving changes the extraction only. Rebuild lots and listings afterwards:
    python -m pipeline.promote_extractions --filename F --rebuild-lots --skip-parcels
    python -m pipeline.apply_extractions --filename F

Run:
    python -m scripts.fill_gaps --only NOTICE.jpg --dry-run
    python -m scripts.fill_gaps --below-score 60 --limit 50 --concurrency 4
    python -m scripts.fill_gaps --keys reserve_price --below-score 100
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from api.neo4j_client import run_read_query
from pipeline.extract_routing import select_extract_model
from pipeline.gap_fill import fill, gaps
from pipeline.keep_better import judge
from pipeline.key_entities import KEYS
from scripts.reset_langextract_and_extract import (
    KeptExisting, _next_batch, _stored, select_only_docs, write_extraction,
)


def select_low(below: int, limit: int | None) -> list[str]:
    rows = run_read_query(
        "MATCH (d:Document) WHERE d.stitched_into IS NULL "
        "  AND d.extraction_json IS NOT NULL AND d.extraction_score < $s "
        "RETURN d.filename AS f ORDER BY d.extraction_score, d.filename",
        {"s": below}, max_rows=20_000, timeout=120.0)
    names = [r["f"] for r in rows]
    return names[:limit] if limit else names


def make_reader(notice_type: str | None):
    from pipeline import langextract_examples as LX
    from pipeline.gap_fill import KEY_CLASS, lean_example, lean_prompt
    from pipeline.load_extractions import _entities
    model_id, reasoning_off = select_extract_model(notice_type)

    def read(text: str, keys: list[str], hint: str | None) -> list[dict]:
        res = LX.extract(text, model_id=model_id, reasoning_off=reasoning_off,
                         passes=1, prompt=lean_prompt(keys, hint),
                         examples=[lean_example({KEY_CLASS[k] for k in keys})])
        return _entities(res, text)
    return read


def fill_one(d: dict, keys: set[str], batch: int, dry_run: bool,
             max_lots: int | None) -> str:
    fn = d["filename"]
    stored = _stored(fn)
    if not stored["entities"]:
        return "no stored extraction"
    if stored["text_changed"]:
        return "text changed since the stored read — needs a full re-read"
    todo = {li: [k for k in ks if k in keys]
            for li, ks in gaps(stored["entities"]).items()}
    if not any(todo.values()):
        return "no gaps"
    read = make_reader(d.get("notice_type"))

    def read_wanted(text, lot_keys, hint):
        return read(text, [k for k in lot_keys if k in keys] or lot_keys, hint)
    filled, report = fill(d["md"], stored["entities"], read_wanted,
                          d.get("expected_lot_count"), max_lots=max_lots)
    ok, gains, losses = judge(stored["entities"], filled, d["md"],
                              d.get("expected_lot_count"))
    head = f"{report['reads']} read(s), {report['failed']} failed"
    if not ok:
        return f"{head}; nothing gained" + (f" (lost {losses[:3]})" if losses else "")
    if dry_run:
        return f"{head}; would gain {', '.join(gains[:6])}"
    write_extraction(d, filled, batch, keep_better=True)
    return f"{head}; saved"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", action="append", default=[], metavar="FILENAME")
    ap.add_argument("--below-score", type=int, default=None,
                    help="every notice whose extraction scores below this")
    ap.add_argument("--keys", default=",".join(KEYS),
                    help=f"key facts to fill (default all: {','.join(KEYS)})")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--max-lots", type=int, default=20,
                    help="at most this many lots read per notice")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true",
                    help="read and report, save nothing")
    args = ap.parse_args(argv)

    keys = {k.strip() for k in args.keys.split(",") if k.strip()}
    bad = keys - set(KEYS)
    if bad:
        print(f"unknown key(s): {', '.join(sorted(bad))}")
        return 2
    names = list(args.only)
    if args.below_score is not None:
        names += select_low(args.below_score, args.limit)
    if not names:
        print("name notices with --only or pick them with --below-score")
        return 2
    docs = select_only_docs(sorted(set(names)))
    batch = 0 if args.dry_run else _next_batch()
    print(f"{len(docs)} notice(s); keys={','.join(sorted(keys))}"
          + ("; dry run" if args.dry_run else f"; batch B{batch}"), flush=True)
    lock, saved, t0 = threading.Lock(), [], time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futs = {ex.submit(fill_one, d, keys, batch, args.dry_run,
                          args.max_lots): d["filename"] for d in docs}
        for i, fut in enumerate(as_completed(futs), 1):
            fn = futs[fut]
            try:
                msg = fut.result()
            except KeptExisting as e:
                msg = f"kept ({e})"
            except Exception as e:  # one notice must not stop the run
                msg = f"error: {type(e).__name__}: {e}"
            if msg.endswith("saved"):
                with lock:
                    saved.append(fn)
            print(f"  [{i}/{len(docs)}] {fn}: {msg}", flush=True)
    print(f"done — saved {len(saved)} of {len(docs)} in "
          f"{(time.time() - t0) / 60:.1f}m")
    if saved:
        print("rebuild lots for: " + " ".join(saved))
    return 0


if __name__ == "__main__":
    sys.exit(main())
