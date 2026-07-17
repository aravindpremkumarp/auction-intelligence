"""
pipeline/ocr_health.py
----------------------
Intrinsic OCR-health scoring for :Document.markdown.

`markdown_quality_score` (pipeline/score_markdown.py) measures how well the
OCR text covers the *scraped website description* — a content-coverage signal
that says nothing reliable about the image→markdown step itself: a perfect
OCR of a notice whose wording diverges from the website scores low, and a
document with no ``website_description`` can't be scored at all.

This module scores the OCR output *by itself*, detecting the failure modes
MinerU's vlm model actually exhibits on full-page ruled notices:

  - **repetition**  — degenerate generation loops (the same line emitted
    dozens of times back-to-back, or a phrase repeated adjacently inside
    one long line). Legitimate notices repeat boilerplate too ("For details
    and queries…" once per lot), but *interleaved* with other content — only
    consecutive runs are flagged.
  - **token-leak**  — raw model control tokens (``<|content_end|>`` …)
    leaking into the text, including the HTML-escaped form MinerU produces
    inside table cells.
  - **truncated**   — generation stopped mid-structure: an opened ``<table>``
    that never closes, output ending inside a tag, or a final table row
    padded with empty cells.

Score = 100 minus per-flag penalties, clamped to 0–100. A document with no
flags scores 100. Fields written (additive — ``markdown_quality_score`` is
untouched):

    d.ocr_health_score  int 0–100 (NULL when there is no markdown)
    d.ocr_health_flags  list of strings, possibly empty
    d.ocr_health_at     datetime of scoring

Usage
~~~~~
    python -m pipeline.ocr_health            # score docs not yet scored
    python -m pipeline.ocr_health --force    # re-score everything
    python -m pipeline.ocr_health --limit 100

`score_freshly_loaded(file_paths)` mirrors pipeline/score_markdown.py and is
called after re-ingest / per-block re-extract writes new markdown.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from typing import Iterable

from api.neo4j_client import run_query, run_read_query


# Only scan the first N chars — health artifacts show up throughout the
# output, and this bounds the (backtracking) within-line regex.
MAX_SCAN_CHARS = 200_000

# ── repetition ──────────────────────────────────────────────────────────────
# Consecutive identical (normalized, non-trivial) lines. Legit boilerplate
# repeats are interleaved with content; loops are back-to-back.
REP_MIN_LINE_CHARS = 6      # ignore trivial lines ("etc.,", "---")
REP_RUN_THRESHOLD = 5       # runs this long or longer flag the doc
# A phrase (12–80 chars) repeated adjacently ≥ 6 times inside one line —
# catches loops in single-line HTML tables that carry no newlines.
REP_INLINE_RE = re.compile(r"(.{12,80}?)(?:\s*\1){5,}")

# ── token leak ──────────────────────────────────────────────────────────────
# Raw or HTML-escaped model control tokens: <|content_end|>, <|im_end|>, …
TOKEN_LEAK_RE = re.compile(r"(?:<|&lt;)\|[a-z][a-z0-9_]*\|(?:>|&gt;)", re.I)

# ── truncation ──────────────────────────────────────────────────────────────
# ≥ 2 empty cells closing the final table row: the generator gave up
# mid-row and padded the structure shut.
TRUNC_EMPTY_TAIL_RE = re.compile(
    r"(?:<td>\s*</td>\s*){2,}</tr>\s*</table>\s*$", re.I)
# Output ends inside an unfinished tag ("…<td" / "…</ta").
TRUNC_OPEN_TAG_RE = re.compile(r"<[a-z/][^>]*$", re.I)

PENALTY = {"repetition": 0, "token-leak": 40, "truncated": 30}  # repetition scaled


def _norm_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip().lower()


def _max_consecutive_run(markdown: str) -> int:
    """Longest run of consecutive identical normalized lines."""
    best = run = 0
    prev: str | None = None
    for raw in markdown.splitlines():
        line = _norm_line(raw)
        if len(line) < REP_MIN_LINE_CHARS:
            prev = None
            run = 0
            continue
        if line == prev:
            run += 1
        else:
            run = 1
            prev = line
        best = max(best, run)
    return best


def score_ocr_health(markdown: str | None) -> dict:
    """Score one document's OCR markdown.

    Returns ``{"score": int|None, "flags": [str], "details": {…}}``.
    ``score`` is None when there is no markdown to judge.
    """
    if not markdown or not markdown.strip():
        return {"score": None, "flags": [], "details": {}}
    text = markdown[:MAX_SCAN_CHARS]

    flags: list[str] = []
    details: dict = {}
    penalty = 0

    run = _max_consecutive_run(text)
    inline = None
    if run < REP_RUN_THRESHOLD:
        m = REP_INLINE_RE.search(text)
        if m:
            inline = m.group(1).strip()[:60]
    if run >= REP_RUN_THRESHOLD or inline:
        flags.append("repetition")
        details["repetition_run"] = run
        if inline:
            details["repetition_inline"] = inline
        penalty += min(50, 10 + 2 * run)

    leak = TOKEN_LEAK_RE.search(text)
    if leak:
        flags.append("token-leak")
        details["token_leak"] = leak.group(0)
        penalty += PENALTY["token-leak"]

    truncated = False
    if text.count("<table") > text.count("</table"):
        truncated = True
        details["truncation"] = "unclosed-table"
    elif TRUNC_EMPTY_TAIL_RE.search(text):
        truncated = True
        details["truncation"] = "empty-tail-cells"
    elif TRUNC_OPEN_TAG_RE.search(text[-120:]):
        truncated = True
        details["truncation"] = "ends-mid-tag"
    if truncated:
        flags.append("truncated")
        penalty += PENALTY["truncated"]

    return {"score": max(0, 100 - penalty), "flags": flags, "details": details}


# ── Neo4j plumbing ──────────────────────────────────────────────────────────

def _fetch_docs(file_paths: list[str] | None, force: bool) -> list[dict]:
    where = ["d.markdown IS NOT NULL", "d.markdown <> ''"]
    params: dict = {}
    if file_paths is not None:
        where.append("d.file_path IN $file_paths")
        params["file_paths"] = file_paths
    if not force:
        where.append("d.ocr_health_score IS NULL")
    cypher = f"""
        MATCH (d:Document)
        WHERE {' AND '.join(where)}
        RETURN d.file_path AS file_path, d.markdown AS markdown
    """
    return run_read_query(cypher, params, max_rows=20_000, timeout=60.0)


def _write_health(rows: list[dict]) -> None:
    """Persist {file_path, score, flags} triples."""
    if not rows:
        return
    run_query(
        """
        UNWIND $rows AS row
        MATCH (d:Document {file_path: row.file_path})
        SET d.ocr_health_score = row.score,
            d.ocr_health_flags = row.flags,
            d.ocr_health_at    = datetime()
        """,
        {"rows": rows},
    )


def score_freshly_loaded(file_paths: Iterable[str]) -> int:
    """Compute and persist OCR-health for the given file_paths. Returns count.

    Always re-scores — the caller just wrote new markdown, so any prior
    health verdict is stale.
    """
    fps = [p for p in file_paths if p]
    if not fps:
        return 0
    docs = _fetch_docs(file_paths=fps, force=True)
    payload = []
    for d in docs:
        h = score_ocr_health(d.get("markdown"))
        payload.append({"file_path": d["file_path"],
                        "score": h["score"], "flags": h["flags"]})
    _write_health(payload)
    return len(payload)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="re-score Documents that already have a health score")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N Documents")
    ap.add_argument("--write-batch", type=int, default=500,
                    help="rows per Neo4j UNWIND write")
    args = ap.parse_args()

    print("Fetching Documents from Neo4j…")
    docs = _fetch_docs(file_paths=None, force=args.force)
    if args.limit:
        docs = docs[: args.limit]
    total = len(docs)
    print(f"Scoring OCR health for {total} Documents (force={args.force})")
    if total == 0:
        return 0

    batch: list[dict] = []
    done = 0
    flagged = 0
    t0 = time.time()
    for d in docs:
        h = score_ocr_health(d.get("markdown"))
        if h["flags"]:
            flagged += 1
        batch.append({"file_path": d["file_path"],
                      "score": h["score"], "flags": h["flags"]})
        done += 1
        if len(batch) >= args.write_batch:
            _write_health(batch)
            batch = []
        if done % 500 == 0 or done == total:
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            print(f"  [{done}/{total}]  rate={rate:.0f}/s  flagged={flagged}",
                  flush=True)
    if batch:
        _write_health(batch)

    print(f"\nScored {done} Documents  flagged={flagged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
