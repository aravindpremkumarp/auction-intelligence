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
  - **degenerate-sequence** — a counting loop: the generator loses the page and
    emits a run of bare numbers instead of the text that is actually there
    ("West by: 26, 27, 28, … 100." in place of a boundary description). Every
    token is *new*, so the repetition checks above — which compare lines and
    phrases for equality — never fire, the output stays well-formed English,
    and the doc scores 100 while a whole schedule's worth of real text is
    gone.
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

Score = 100 minus per-flag penalties, clamped to 0–100. A document with no
flags scores 100. Fields written (additive — ``markdown_quality_score`` is
untouched):

    d.ocr_health_score     int 0–100 (NULL when there is no markdown)
    d.ocr_health_flags     list of strings, possibly empty
    d.ocr_health_at        datetime of scoring
    d.ink_uncovered_ratio  float 0–1, only when the page's ink was measured
    d.ink_coverage_at      datetime of that measurement

Usage
~~~~~
    python -m pipeline.ocr_health            # score docs not yet scored
    python -m pipeline.ocr_health --force    # re-score everything
    python -m pipeline.ocr_health --limit 100

The CLI is the bulk, text-only pass (it never fetches images — for a corpus
ink sweep see scripts/score_ink_coverage.py). `score_freshly_loaded(file_paths)`
mirrors pipeline/score_markdown.py and is called after the loader, a re-ingest,
a per-block re-extract or a block edit writes new markdown/blocks; being
per-document, it DOES fetch the source and fold ``missing-region`` in.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from typing import Iterable

from api.neo4j_client import run_query, run_read_query

log = logging.getLogger(__name__)


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

# ── degenerate numeric sequence ──────────────────────────────────────────────
# The repetition checks above test for *equality* — identical lines, an
# identical phrase repeated adjacently. A counting loop defeats both: every
# item differs from the last, so "26, 27, 28, … 100" (75 items, observed in
# place of a Schedule-F boundary description) reads as ordinary prose and
# scores 100. This detector looks at the *shape* of a numeric run instead.
#
# Segments are split on line breaks, HTML tags and markdown pipes so a run
# can never span table cells: a grid whose cells hold 1, 2, 3, … is normal
# layout, not a loop, and each cell is judged on its own.
SEQ_SEGMENT_SPLIT_RE = re.compile(r"<[^>]+>|[|\n\r]+")
# A comma/semicolon-separated run of bare integers. Grouped amounts match too
# ("2,34,00,000" → 4 items) and are far below both thresholds.
SEQ_NUM_RUN_RE = re.compile(r"\d{1,4}(?:\s*[,;]\s*\d{1,4})+")
# Strictly +1 ascending items this long → a counting loop.
#
# Set from a sweep of the 1622-document corpus, which was emphatic that a long
# ascending run is NOT by itself a fault: a DTCP-approved layout really does
# enumerate its plots ("Plot Nos.1026, 1027, … and 1053, as approved by DTCP
# No.30 of 2006"), and the longest such legitimate list runs to 27 items. The
# one true loop in the corpus runs to 504 ("Door No. 497, 498, … " for a single
# A.C.C. shell building, the description never finishing before the cell ends),
# and the notice this detector was written for ran to 75. 60 sits in that gap
# with room on both sides; it costs the short loops, which is the right trade
# when the alternative is flagging real notices.
SEQ_MIN_STEP_RUN = 60
# ...and a long list that is *not* ascending is still not prose. The longest
# unordered run in the corpus is 16 items (a list of survey numbers), and the
# longest run of any shape that reads as real content is the 27-plot list above.
SEQ_MIN_ITEMS = 40


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
           # Priced with table-collapse: both keep a well-formed document
           # while the notice's real words stop being in it.
           "degenerate-sequence": 35,
           # Lost content is the worst outcome for downstream extraction: the
           # properties in an unread column simply do not exist for us. Priced
           # above table-collapse, which at least keeps the text.
           "missing-region": 45}  # repetition scaled

# The canonical failure vocabulary, in severity order. The review API validates
# its flag filter against this, so a renamed or added flag reaches the UI by
# changing this module alone — no second list to drift out of sync.
HEALTH_FLAGS: tuple[str, ...] = (
    "missing-region", "table-collapse", "degenerate-sequence", "truncated",
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


def _max_step_run(items: list[int]) -> int:
    """Longest run of items each exactly one more than the one before."""
    best = run = 1
    for prev, cur in zip(items, items[1:]):
        run = run + 1 if cur == prev + 1 else 1
        best = max(best, run)
    return best


def _degenerate_sequence(text: str) -> dict | None:
    """Detect a numeric counting loop. Returns detail dict or ``None``.

    Scans each segment (see :data:`SEQ_SEGMENT_SPLIT_RE`) for comma-separated
    runs of bare integers and reports the worst one: a run climbing by one for
    :data:`SEQ_MIN_STEP_RUN` items, or any run of :data:`SEQ_MIN_ITEMS` items.
    Returns the offending run's length, its longest ascending stretch and a
    short sample, so the reviewer can see what the model wrote instead.
    """
    worst: dict | None = None
    for segment in SEQ_SEGMENT_SPLIT_RE.split(text):
        for m in SEQ_NUM_RUN_RE.finditer(segment):
            items = [int(n) for n in re.findall(r"\d+", m.group(0))]
            step_run = _max_step_run(items)
            if step_run < SEQ_MIN_STEP_RUN and len(items) < SEQ_MIN_ITEMS:
                continue
            hit = {"items": len(items), "step_run": step_run,
                   "sample": re.sub(r"\s+", " ", m.group(0)).strip()[:60]}
            if worst is None or (hit["step_run"], hit["items"]) > (
                    worst["step_run"], worst["items"]):
                worst = hit
    return worst


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


def score_ocr_health(markdown: str | None, *, region: dict | None = None) -> dict:
    """Score one document's OCR markdown.

    Returns ``{"score": int|None, "flags": [str], "details": {…}}``.
    ``score`` is None when there is no markdown to judge.

    ``region`` is an optional :func:`pipeline.ink_coverage.score_ink_coverage`
    result. It is passed in rather than computed here because it needs the
    source image, which this module (pure text, called in bulk over Neo4j rows)
    deliberately never fetches. Omitted or unscorable → no ``missing-region``
    flag, and every existing caller keeps its exact behaviour.
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

    sequence = _degenerate_sequence(text)
    if sequence:
        flags.append("degenerate-sequence")
        details["degenerate_sequence"] = sequence
        penalty += PENALTY["degenerate-sequence"]

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

    return {"score": max(0, 100 - penalty), "flags": flags, "details": details}


# Flags that are meaningful for a single block: the intrinsic per-fragment
# artifacts. `table-collapse` and `truncated` are whole-document / structure
# concerns (one block being a table is normal), so they are not evaluated here.
BLOCK_HEALTH_FLAGS = ("degenerate-sequence", "repetition", "token-leak",
                      "foreign-script")


def score_block_health(text: str | None) -> dict:
    """Score one block's text for the per-fragment OCR artifacts.

    Same detectors as :func:`score_ocr_health` for repetition, token-leak,
    foreign-script and degenerate-sequence, but scoped to a single block so the
    annotator can tag the exact block a loop/leak/hallucination comes from.
    Document-structure flags (table-collapse, truncated) are intentionally not
    evaluated per block.

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

    sequence = _degenerate_sequence(t)
    if sequence:
        flags.append("degenerate-sequence")
        details["degenerate_sequence"] = sequence
        penalty += PENALTY["degenerate-sequence"]

    return {"score": max(0, 100 - penalty), "flags": flags, "details": details}


# ── Neo4j plumbing ──────────────────────────────────────────────────────────

def _fetch_docs(file_paths: list[str] | None, force: bool,
                *, with_source: bool = False) -> list[dict]:
    """Documents to score. ``with_source`` also pulls what the ink measure
    needs (blocks, source URL, block provenance) — heavy columns, so the bulk
    text-only pass leaves them out."""
    where = ["d.markdown IS NOT NULL", "d.markdown <> ''"]
    params: dict = {}
    if file_paths is not None:
        where.append("d.file_path IN $file_paths")
        params["file_paths"] = file_paths
    if not force:
        where.append("d.ocr_health_score IS NULL")
    extra = (", d.blocks AS blocks_json, d.public_url AS public_url, "
             "d.blocks_source AS blocks_source, d.filename AS filename"
             if with_source else "")
    cypher = f"""
        MATCH (d:Document)
        WHERE {' AND '.join(where)}
        RETURN d.file_path AS file_path, d.markdown AS markdown{extra}
    """
    return run_read_query(cypher, params, max_rows=20_000, timeout=60.0)


def _write_health(rows: list[dict]) -> None:
    """Persist {file_path, score, flags} rows.

    A row carrying ``ink_scored: True`` also persists its ``ratio`` as
    ``ink_uncovered_ratio`` — the page total behind the ``missing-region``
    flag, which the review queue shows in the health pill's tooltip. Rows
    without it (text-only scoring, or a page that couldn't be measured) leave
    the ink fields exactly as they were.
    """
    if not rows:
        return
    run_query(
        """
        UNWIND $rows AS row
        MATCH (d:Document {file_path: row.file_path})
        SET d.ocr_health_score = row.score,
            d.ocr_health_flags = row.flags,
            d.ocr_health_at    = datetime()
        FOREACH (_ IN CASE WHEN coalesce(row.ink_scored, false) THEN [1] ELSE [] END |
            SET d.ink_uncovered_ratio = row.ratio,
                d.ink_coverage_at     = datetime())
        """,
        {"rows": rows},
    )


# ── ink coverage on the live path ────────────────────────────────────────────
# ``score_ocr_health`` takes the ink verdict as an argument because it judges
# text and never fetches an image. That left the live path — the loader, a
# re-ingest, a per-block re-extract, a reviewer's block edit — scoring text
# only: every one of them called ``score_freshly_loaded``, which never passed
# ``region``, so ``missing-region`` was written by the offline scripts alone
# (scripts/score_ink_coverage.py, scripts/fix_missing_regions.py). Any notice
# touched after those ran went back to health 100 with no flag, while the
# annotator's Ink tab — measuring live — showed a 50% unread patch on the same
# page. ``score_freshly_loaded`` is per-document and already the moment the
# blocks changed, so it is where the measure belongs.

# The source is fetched from R2; a broken or absent one must not stop the
# text score from being written. Same cap the annotator's Ink tab applies.
INK_SOURCE_MAX_BYTES = 32 * 1024 * 1024
INK_SOURCE_TIMEOUT_S = 60
# Documents backfilled by scripts/backfill_blocks_datalab.py carry Datalab
# blocks over MinerU markdown. Coverage there measures the Datalab parse, not
# the text we store, so folding it into ocr_health would attribute one
# engine's miss to the other's output — the same exclusion the corpus scorer
# applies; their reading lives in shadow_ink_uncovered_ratio instead.
INK_SKIP_BLOCK_SOURCES = ("datalab-backfill",)


def _blocks_from_json(raw: str | None) -> list[dict]:
    """The block list out of the stored ``d.blocks`` blob (dict or bare list)."""
    if not raw:
        return []
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(obj, dict):
        obj = obj.get("blocks")
    return [b for b in obj if isinstance(b, dict)] if isinstance(obj, list) else []


def _fetch_source(url: str) -> bytes | None:
    """The notice's source bytes, or ``None`` when they can't be had."""
    import requests
    try:
        resp = requests.get(url, timeout=INK_SOURCE_TIMEOUT_S)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("ink source fetch failed for %s: %s", url, type(e).__name__)
        return None
    if len(resp.content) > INK_SOURCE_MAX_BYTES:
        log.warning("ink source too large for %s (%d bytes)", url, len(resp.content))
        return None
    return resp.content


def region_for(doc: dict) -> dict | None:
    """The ``region`` argument for one fetched document, or ``None``.

    ``None`` means "don't judge the ink": no blocks, no source URL, blocks
    from another engine than the markdown, or a source that couldn't be
    fetched. The text score is still written in every one of those cases —
    only the ``missing-region`` verdict is withheld. A measured-but-unscorable
    page (too little ink, unreadable file) comes back as a dict with
    ``uncovered_ratio`` None, which ``score_ocr_health`` treats the same way.
    """
    if (doc.get("blocks_source") or "") in INK_SKIP_BLOCK_SOURCES:
        return None
    blocks = _blocks_from_json(doc.get("blocks_json"))
    url = doc.get("public_url")
    if not blocks or not url:
        return None
    img = _fetch_source(url)
    if img is None:
        return None
    from pipeline.ink_coverage import score_document_ink
    try:
        return score_document_ink(img, blocks)
    except Exception:                                # never block the text score
        log.exception("ink coverage failed for %s", doc.get("filename") or url)
        return None


def score_freshly_loaded(file_paths: Iterable[str]) -> int:
    """Compute and persist OCR-health for the given file_paths. Returns count.

    Always re-scores — the caller just wrote new markdown or blocks, so any
    prior health verdict is stale. This is the per-document path (loader,
    re-ingest, re-extract, block edits), so it also measures the page's ink
    against the stored blocks and folds the ``missing-region`` verdict in;
    see :func:`region_for` for when that is skipped.
    """
    fps = [p for p in file_paths if p]
    if not fps:
        return 0
    docs = _fetch_docs(file_paths=fps, force=True, with_source=True)
    payload = []
    for d in docs:
        region = region_for(d)
        h = score_ocr_health(d.get("markdown"), region=region)
        row = {"file_path": d["file_path"],
               "score": h["score"], "flags": h["flags"]}
        if region is not None and region.get("uncovered_ratio") is not None:
            row["ink_scored"] = True
            row["ratio"] = region["uncovered_ratio"]
        payload.append(row)
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
