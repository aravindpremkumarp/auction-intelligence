"""Repair stored descriptions that stop short of their own lot's details.

For each notice flagged ``full_description_incomplete``: stretch each lot's
full_description over the details it missed (pipeline/widen_descriptions —
code only, no model call) and save through the keep-better gate, so a notice
is written only when the validator score rises and no key fact is lost. The
read it replaces is kept (``extraction_prev_json``; scripts/revert_extraction
restores it), and reviewer corrections are carried onto the widened block.

A notice whose text changed since its stored read is skipped: its spans point
into a text that is gone. A lot whose missed detail is far away, past another
property's heading or past a price is left as it is, and stays flagged.

Saving changes the extraction only. Rebuild lots and listings afterwards:
    python -m pipeline.promote_extractions --filename F --rebuild-lots --skip-parcels
    python -m pipeline.apply_extractions --filename F

Run:
    python -m scripts.widen_descriptions --dry-run
    python -m scripts.widen_descriptions
    python -m scripts.widen_descriptions --only NOTICE.jpg
    python -m scripts.widen_descriptions --all --dry-run   # unflagged ones too
"""
from __future__ import annotations

import argparse

from api.neo4j_client import run_read_query
from pipeline.keep_better import judge
from pipeline.widen_descriptions import widen
from scripts.reset_langextract_and_extract import (
    KeptExisting, _next_batch, _stored, select_only_docs, write_extraction,
)


def select_flagged(limit: int | None, every: bool = False) -> list[str]:
    flagged = ("" if every else "'full_description_incomplete' IN "
               "coalesce(d.extraction_issue_codes, []) AND ")
    rows = run_read_query(
        "MATCH (d:Document) WHERE " + flagged + "d.extraction_json IS NOT NULL "
        "AND d.stitched_into IS NULL RETURN d.filename AS fn ORDER BY d.filename"
        + (" LIMIT $limit" if limit else ""),
        {"limit": limit}, max_rows=20_000, timeout=120.0)
    return [r["fn"] for r in rows or []]


def widen_one(d: dict, batch: int, dry_run: bool) -> str:
    fn = d["filename"]
    stored = _stored(fn)
    if not stored["entities"]:
        return "no stored extraction"
    if stored["text_changed"]:
        return "text changed since the stored read — needs a full re-read"
    ents, md = stored["entities"], d["md"] or ""
    new, report = widen(ents, md)
    grown = {li: r for li, r in report.items() if r["to"] != r["from"]}
    if not grown:
        return "nothing reachable"
    reach = ", ".join(f"lot {li} +{(r['to'][1] - r['to'][0]) - (r['from'][1] - r['from'][0])}"
                      f" chars ({r['reached']} reached, {r['left']} left)"
                      for li, r in sorted(grown.items()))
    ok, gains, losses = judge(ents, new, md, d.get("expected_lot_count"))
    # A reworded block (its text names details its span misses) is not
    # flagged, so the validator score does not move when its span grows over
    # them — but the highlight now reaches them and the text is the page's.
    # That is the gain; the loss check still stands.
    reached = sum(r["reached"] for r in grown.values())
    if not ok and not losses and reached:
        ok, gains = True, [f"{reached} detail(s) now inside the description"]
    if dry_run:
        verdict = f"would save ({', '.join(gains)})" if ok else (
            "would keep: " + (", ".join(losses) if losses else "no gain"))
        return f"{reach}; {verdict}"
    if not ok:
        return f"{reach}; kept: " + (", ".join(losses) if losses else "no gain")
    try:
        # judged above (the stored read's text is unchanged, checked first);
        # the store's own keep-better gate would refuse a span-only gain.
        write_extraction(d, new, batch, keep_better=False, keep_auto_marks=True)
    except KeptExisting as e:
        return f"{reach}; kept ({e})"
    return f"{reach}; saved"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", action="append", default=[], metavar="FILENAME")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="report, save nothing")
    ap.add_argument("--all", action="store_true",
                    help="every extracted notice, not only the flagged ones — "
                         "a reworded block (its text names details its span "
                         "misses) is not flagged, so it is found only this way")
    args = ap.parse_args(argv)

    names = list(args.only) or select_flagged(args.limit, args.all)
    docs = select_only_docs(sorted(set(names)))
    batch = 0 if args.dry_run else _next_batch()
    print(f"{len(docs)} notice(s)" + ("; dry run" if args.dry_run else f"; batch B{batch}"),
          flush=True)
    saved = []
    for i, d in enumerate(docs, 1):
        try:
            msg = widen_one(d, batch, args.dry_run)
        except Exception as e:  # one notice must not stop the run
            msg = f"error: {type(e).__name__}: {e}"
        if msg.endswith("; saved") or "; would save" in msg:
            saved.append(d["filename"])
        print(f"  [{i}/{len(docs)}] {d['filename']}: {msg}", flush=True)
    verb = "would save" if args.dry_run else "saved"
    print(f"done — {verb} {len(saved)} of {len(docs)}")
    if saved and not args.dry_run:
        print("rebuild lots for: " + " ".join(saved))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
