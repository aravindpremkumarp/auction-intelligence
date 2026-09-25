"""Work every "not found" price, date and description without a person.

scripts/fill_gaps marks a must-have fact two reads missed as *unfound*, with
the reason the lot's text gives (pipeline/absence.must_have_rule):

* ``lost_from_text`` — the text has no amount / date / description words for
  the lot: OCR probably dropped it;
* ``read_missed``   — the text has them: the reading went wrong.

For each notice holding such marks this script, in order:

1. **Image check** — scripts/fix_missing_regions re-measures every page of the
   notice (its joined pages too) against the image and re-OCRs only an unread
   patch. A clean page costs nothing but the measurement.
2. **Full re-read** — the whole notice read again, through the keep-better
   gate (it is saved only if it gains and loses nothing), when the text was
   repaired or a fact was ``read_missed``.
3. **Gap-filler** — short reads for what is still missing.

Whatever is still missing after all three is marked ``needs_person``: the
review page shows it as "checked text and image — needs you", and no run pays
for it again until the notice's text changes.

Writing changes extractions and, for a repaired page, the markdown. Rebuild
lots and listings afterwards for the notices it lists.

Run:
    python -m scripts.resolve_unfound --dry-run          # what it would do
    python -m scripts.resolve_unfound --limit 10
    python -m scripts.resolve_unfound --only F.jpg
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from api.neo4j_client import run_read_query
from pipeline.absence import (
    AUTO, RULE_LOST_FROM_TEXT, RULE_NEEDS_PERSON, RULE_READ_MISSED, write_marks,
)
from pipeline.key_entities import key_marks, unfound_key

OPEN_RULES = (RULE_LOST_FROM_TEXT, RULE_READ_MISSED)


def open_marks(corrections: dict) -> dict[tuple[str, str], str]:
    """(lot, key) -> rule, for automatic unfound marks still to be worked."""
    return {at: m.get("rule") for at, m in key_marks(corrections).items()
            if m["kind"] == "unfound" and m.get("by") == AUTO
            and m.get("rule") in OPEN_RULES}


def select(only: list[str], limit: int | None) -> list[dict]:
    rows = run_read_query(
        "MATCH (d:Document) WHERE d.stitched_into IS NULL "
        "  AND d.extraction_corrections_json CONTAINS 'unfound:' "
        + ("AND d.filename IN $only " if only else "") +
        "RETURN d.filename AS f, d.extraction_corrections_json AS c, "
        "       coalesce(d.stitched_pages, []) AS pages "
        "ORDER BY d.filename",
        {"only": only}, max_rows=20_000, timeout=120.0)
    out = []
    for r in rows:
        try:
            marks = open_marks(json.loads(r["c"] or "{}"))
        except (TypeError, ValueError):
            continue
        if marks:
            out.append({"filename": r["f"], "marks": marks,
                        "pages": [p for p in r["pages"] if p] or [r["f"]]})
    return out[:limit] if limit else out


def image_check(pages: list[str]) -> tuple[bool, str]:
    """Re-OCR any unread patch on these pages. ``(text_changed, note)``."""
    from scripts import fix_missing_regions as F
    if not os.environ.get("DATALAB_API_KEY"):
        return False, "image check skipped (DATALAB_API_KEY not set)"
    results = [F.fix_one(t, margin=F.DEFAULT_MARGIN) for t in F.select_named(pages)]
    written = F.write_back(results)
    notes = "; ".join(f"{r['filename']}: {r['note'] or 'recovered'}" for r in results)
    return written > 0, notes or "no page to check"


def reread(filename: str, batch: int) -> str:
    """A full re-read through the keep-better gate."""
    from scripts.reset_langextract_and_extract import (
        KeptExisting, _extract_one, select_only_docs,
    )
    docs = select_only_docs([filename])
    if not docs:
        return "not found"
    try:
        _extract_one(docs[0], batch, route=True, keep_better=True)
        return "re-read saved"
    except KeptExisting:
        return "re-read not better — kept"
    except Exception as e:  # one notice must not stop the run
        return f"re-read failed: {type(e).__name__}"


def still_missing(filename: str, asked: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Of the facts ``asked`` about, those the stored read still lacks."""
    from pipeline.gap_fill import gaps
    rows = run_read_query(
        "MATCH (d:Document {filename:$f}) RETURN d.extraction_json AS j", {"f": filename})
    ents = json.loads(rows[0]["j"] or "[]") if rows else []
    g = gaps(ents)
    return {(lot, k) for lot, k in asked if k in g.get(lot, [])}


def resolve(t: dict, batch: int, dry_run: bool) -> str:
    fn, marks = t["filename"], t["marks"]
    lost = any(r == RULE_LOST_FROM_TEXT for r in marks.values())
    missed = any(r == RULE_READ_MISSED for r in marks.values())
    if dry_run:
        return (f"{len(marks)} open ({sum(r == RULE_LOST_FROM_TEXT for r in marks.values())} "
                f"lost from text, {sum(r == RULE_READ_MISSED for r in marks.values())} "
                f"read missed): image check on {len(t['pages'])} page(s)"
                + ("; full re-read" if lost or missed else ""))
    steps = []
    changed, note = image_check(t["pages"])
    steps.append(f"image: {note[:120]}")
    if changed or missed:
        steps.append(reread(fn, batch))
        from scripts.fill_gaps import fill_one
        from scripts.reset_langextract_and_extract import select_only_docs
        from pipeline.key_entities import KEYS
        docs = select_only_docs([fn])
        if docs:
            try:
                steps.append("gaps: " + fill_one(docs[0], set(KEYS), batch,
                                                 dry_run=False, max_lots=20))
            except Exception as e:
                steps.append(f"gaps failed: {type(e).__name__}")
    left = still_missing(fn, set(marks))
    if left:
        from datetime import datetime, timezone
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_marks(fn, {unfound_key(lot, k): {"by": AUTO, "rule": RULE_NEEDS_PERSON,
                                               "at": at} for lot, k in left})
    steps.append(f"{len(marks) - len(left)} found, {len(left)} need a person")
    return " | ".join(steps)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", action="append", default=[], metavar="FILENAME")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    todo = select(args.only, args.limit)
    from scripts.reset_langextract_and_extract import _next_batch
    batch = 0 if args.dry_run else _next_batch()
    print(f"{len(todo)} notice(s) with open not-found facts"
          + ("; dry run" if args.dry_run else f"; batch B{batch}"), flush=True)
    touched = []
    for i, t in enumerate(todo, 1):
        msg = resolve(t, batch, args.dry_run)
        print(f"  [{i}/{len(todo)}] {t['filename']}: {msg}", flush=True)
        if not args.dry_run:
            touched.append(t["filename"])
    if touched:
        print("rebuild lots for: " + " ".join(touched))
    return 0


if __name__ == "__main__":
    sys.exit(main())
