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
  - **foreign-script** — characters from scripts that can never legitimately
    appear in a Tamil Nadu auction notice (CJK, kana, hangul, Cyrillic,
    Arabic, Thai). The vlm model's training prior leaks whole Chinese
    phrases into low-confidence regions ("中国银行股份有" — Bank of China —
    hallucinated into an HDB notice). Tamil, Devanagari and Latin are of
    course fine and never flag.
  - **table-collapse** — the whole notice read as ONE giant HTML ``<table>``.
    On a fully-bordered notice the vlm model swallows the prose above/below
    the grid into table cells; the output is well-formed (closes cleanly, no
    repetition, no leak) so every other check passes and it scores 100 — yet
    most of the notice's text is lost to the reader and to downstream
    extraction. Flagging it routes the doc to the auto-region re-ingest path
    (``scripts/auto_region_reingest.py`` selects health-flagged docs), which
    crops prose / grid / footer bands and OCRs them separately into distinct
    Text/Table/Footer blocks.

  - **missing-region** — ink on the page that no parsed block covers. This is
    the one failure mode the text alone cannot reveal: the checks above all
    judge what we *did* read, so a notice whose entire right-hand column never
    reached the markdown passes every one of them and scores 100. Measured by
    ``pipeline/ink_coverage.py`` (which needs the source image, so it is passed
    in via the ``region`` argument rather than computed here) and flagged above
    ``MISSING_REGION_MIN_RATIO`` of unread ink.

  - **block-order** — the blocks were read, but out of reading order. The text
    checks cannot see this either: an interleaved two-column notice covers all
    of its ink and is well-formed, yet the markdown pairs each lot with its
    neighbour's details. Measured by ``pipeline/block_order.py`` against the
    page's own columns and passed in via ``order``, for the same reason
    ``region`` is: it needs the source image.

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

# ── foreign script ──────────────────────────────────────────────────────────
# Scripts that never appear in a TN auction notice. Notices are English/Tamil
# (occasionally Devanagari); those ranges are deliberately absent here. Any
# hit is vlm prior-leakage — a corpus scan found 17/1190 documents carrying
# hallucinated Chinese, up to 466 chars of it in one notice.
FOREIGN_SCRIPT_RE = re.compile(
    "["
    "一-鿿㐀-䶿"   # CJK unified + extension A
    "぀-ヿ"                # hiragana + katakana
    "가-힯"                # hangul
    "Ѐ-ӿ"                # cyrillic
    "؀-ۿ"                # arabic
    "฀-๿"                # thai
    "]"
)

# ── single-table collapse ────────────────────────────────────────────────────
# A fully-ruled notice comes back as a lone, well-formed <table> holding
# essentially all of the document's text. We flag it when there is exactly one
# table, that table is substantial, and almost none of the visible text lives
# outside it. Thresholds favour precision: a faithfully decomposed notice keeps
# its prose OUTSIDE the grid (markdown paragraphs) and often carries more than
# one table, so it clears every gate below and never flags.
TABLE_TAG_RE = re.compile(r"<table\b.*?</table>", re.I | re.S)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
# Below this share of visible text outside the lone table → collapse.
COLLAPSE_MAX_OUTSIDE_RATIO = 0.15
# ...but never flag a doc with a small table (legitimate short grids, tests).
COLLAPSE_MIN_TABLE_CHARS = 500

PENALTY = {"repetition": 0, "token-leak": 40, "truncated": 30,
           "foreign-script": 40, "table-collapse": 35,
           # Lost content is the worst outcome for downstream extraction: the
           # properties in an unread column simply do not exist for us. Priced
           # above table-collapse, which at least keeps the text.
           "missing-region": 45,
           # Every word is present, so this is priced below the flags that lose
           # content — but not far below: markdown is assembled in block order
           # and extraction reads lot details positionally, so a scrambled
           # sequence silently attaches each lot to the wrong numbers, which is
           # worse to consume than an obvious gap.
           "block-order": 25}  # repetition scaled

# The canonical failure vocabulary, in severity order. The review API validates
# its flag filter against this, so a renamed or added flag reaches the UI by
# changing this module alone — no second list to drift out of sync.
HEALTH_FLAGS: tuple[str, ...] = (
    "missing-region", "table-collapse", "truncated", "block-order",
    "repetition", "token-leak", "foreign-script",
)


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


def _visible_len(s: str) -> int:
    """Non-whitespace character count after stripping HTML tags.

    Approximates the reader-visible text of an HTML/markdown fragment so a
    cell's worth of ``<td>`` wrapping doesn't count toward content length.
    """
    return len(re.sub(r"\s+", "", _TAG_STRIP_RE.sub(" ", s)))


def _table_collapse(text: str) -> dict | None:
    """Detect a single-table collapse. Returns detail dict or ``None``.

    Collapse := exactly one closed ``<table>`` whose visible text is large
    (``COLLAPSE_MIN_TABLE_CHARS``) and holds all but a tiny share
    (``COLLAPSE_MAX_OUTSIDE_RATIO``) of the document's visible text. An
    unclosed table won't match here — it is already caught by the truncation
    check — so a truncated collapse still flags (as ``truncated``) and still
    routes to re-ingest.
    """
    tables = TABLE_TAG_RE.findall(text)
    if len(tables) != 1:
        return None
    table_visible = _visible_len(tables[0])
    if table_visible < COLLAPSE_MIN_TABLE_CHARS:
        return None
    outside_visible = _visible_len(TABLE_TAG_RE.sub(" ", text))
    total_visible = table_visible + outside_visible
    if total_visible == 0:
        return None
    outside_ratio = outside_visible / total_visible
    if outside_ratio >= COLLAPSE_MAX_OUTSIDE_RATIO:
        return None
    return {
        "table_chars": table_visible,
        "outside_chars": outside_visible,
        "outside_ratio": round(outside_ratio, 3),
    }


def score_ocr_health(markdown: str | None, *, region: dict | None = None,
                     order: dict | None = None) -> dict:
    """Score one document's OCR markdown.

    Returns ``{"score": int|None, "flags": [str], "details": {…}}``.
    ``score`` is None when there is no markdown to judge.

    ``region`` is an optional :func:`pipeline.ink_coverage.score_ink_coverage`
    result. It is passed in rather than computed here because it needs the
    source image, which this module (pure text, called in bulk over Neo4j rows)
    deliberately never fetches. Omitted or unscorable → no ``missing-region``
    flag, and every existing caller keeps its exact behaviour.

    ``order`` is the same arrangement for
    :func:`pipeline.block_order.score_block_order`: image-derived, passed in,
    and omitted by default so no existing caller's score moves.
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

    foreign = FOREIGN_SCRIPT_RE.findall(text)
    if foreign:
        flags.append("foreign-script")
        details["foreign_script_count"] = len(foreign)
        details["foreign_script_sample"] = "".join(foreign[:10])
        penalty += PENALTY["foreign-script"]

    collapse = _table_collapse(text)
    if collapse:
        flags.append("table-collapse")
        details["table_collapse"] = collapse
        penalty += PENALTY["table-collapse"]

    if region and region.get("flag"):
        flags.append("missing-region")
        details["missing_region"] = region.get("details") or {
            "uncovered_ratio": region.get("uncovered_ratio")}
        penalty += PENALTY["missing-region"]

    if order and order.get("flag"):
        flags.append("block-order")
        details["block_order"] = order.get("details") or {
            "inversion_ratio": order.get("inversion_ratio")}
        penalty += PENALTY["block-order"]

    return {"score": max(0, 100 - penalty), "flags": flags, "details": details}


# Flags that are meaningful for a single block: the intrinsic per-fragment
# artifacts. `table-collapse` and `truncated` are whole-document / structure
# concerns (one block being a table is normal), so they are not evaluated here.
BLOCK_HEALTH_FLAGS = ("repetition", "token-leak", "foreign-script")


def score_block_health(text: str | None) -> dict:
    """Score one block's text for the per-fragment OCR artifacts.

    Same detectors as :func:`score_ocr_health` for repetition, token-leak and
    foreign-script, but scoped to a single block so the annotator can tag the
    exact block a loop/leak/hallucination comes from. Document-structure flags
    (table-collapse, truncated) are intentionally not evaluated per block.

    Returns ``{"score": int|None, "flags": [str], "details": {…}}``; ``score``
    is None when the block has no text.
    """
    if not text or not text.strip():
        return {"score": None, "flags": [], "details": {}}
    t = text[:MAX_SCAN_CHARS]

    flags: list[str] = []
    details: dict = {}
    penalty = 0

    run = _max_consecutive_run(t)
    inline = None
    if run < REP_RUN_THRESHOLD:
        m = REP_INLINE_RE.search(t)
        if m:
            inline = m.group(1).strip()[:60]
    if run >= REP_RUN_THRESHOLD or inline:
        flags.append("repetition")
        details["repetition_run"] = run
        if inline:
            details["repetition_inline"] = inline
        penalty += min(50, 10 + 2 * run)

    leak = TOKEN_LEAK_RE.search(t)
    if leak:
        flags.append("token-leak")
        details["token_leak"] = leak.group(0)
        penalty += PENALTY["token-leak"]

    foreign = FOREIGN_SCRIPT_RE.findall(t)
    if foreign:
        flags.append("foreign-script")
        details["foreign_script_count"] = len(foreign)
        details["foreign_script_sample"] = "".join(foreign[:10])
        penalty += PENALTY["foreign-script"]

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
