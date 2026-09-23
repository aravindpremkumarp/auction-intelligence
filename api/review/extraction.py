"""
api/review/extraction.py
------------------------
Admin-only `/review/extraction/*` endpoints — the editable, grounded review
surface for LangExtract structured output (see pipeline/langextract_examples.py).

Mirrors the existing description/markdown review idiom (api/review/router.py +
queries.py): read a document's extraction, let a reviewer correct individual
fields, then verify/unverify — all written back to Neo4j with by-email + status
flags so corrections are auditable and can grow the eval gold set.

Data model (on :Document, populated by pipeline/load_extractions.py):
  extraction_json              JSON [{id, cls, text, start, end, attrs}]
  extraction_corrections_json  JSON {field_id: {value, by, at, notes},
                                     "add:<id>": {cls, text, start, end, attrs, by, at},
                                     "absent:<lot>:<key>": {by, at}}
                               — text fixes, reviewer-added entities, and
                               "not in the notice" marks (pipeline/key_entities.py)
  extraction_review_status     'pending' | 'edited' | 'verified'
  extraction_verified_by / _at
  extraction_review_notes      free text left at verify time
  extraction_key_score         0-100: share of per-lot key entities filled or
                               marked absent (pipeline/key_entities.py); the
                               queue's sort=keys order
  extraction_key_missing       count of key cells still missing
  extraction_issue_codes       pipeline/validators.py issue codes (corrections
                               applied) — the queue's failure filters
  extraction_lot_count         lots the model emitted — the "missing lots" /
                               "extra lots" filters, against expected_lot_count

This module is intentionally self-contained (its own APIRouter) so it can be
mounted alongside the main review router without touching the large queries.py.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.auth.dependencies import get_current_admin
from api.auth.schemas import UserOut
from api.neo4j_client import run_query, run_read_query
from api.review.grounding import ANCHOR_STORED, reanchor
from api.review.queries import _date_exists_clause, _notice_type_clause
from pipeline.apply_extractions import ADDED_PREFIX, added_entities
from pipeline.key_entities import (KEYS, absent_key, key_checklist_from_stored,
                                   stamp_key_scores)

# The extraction classes a reviewer may add by hand — the prompt's own list
# (pipeline/langextract_examples.py), so an added entity is shaped like a
# model one and flows through promotion unchanged.
ENTITY_CLASSES = frozenset({
    "secured_creditor", "contact", "borrower", "property", "full_description",
    "location", "identifier", "extent", "boundary", "schedule", "auction_terms",
    "outstanding", "emd_account", "full_terms", "extras",
})

router = APIRouter(prefix="/review/extraction", tags=["review-extraction"])


# ── models ───────────────────────────────────────────────────────────────────
class ExtractionField(BaseModel):
    id: str
    cls: str
    text: str
    start: int | None = None
    end: int | None = None
    grounded: bool = True
    # How the span above was arrived at (api/review/grounding.py): 'stored' —
    # the extraction's own offsets still land on this text; 'relocated' — the
    # markdown moved and the text was found verbatim elsewhere; 'fuzzy' — a
    # similarity match, so the OCR itself changed the characters; 'lost' — not
    # found, span dropped rather than left pointing at the wrong passage;
    # 'none' — never grounded. Anything but 'stored' is worth showing the
    # reviewer, since it means the anchor is ours and not the extraction's.
    anchor: str = "stored"
    lot_index: str | None = None
    attrs: dict = {}
    corrected_value: str | None = None
    corrected_by: str | None = None
    corrected_at: str | None = None
    # The reviewer added this entity by hand (an "add:*" correction): the model
    # never emitted it, so it can be deleted outright rather than corrected.
    added: bool = False


class KeyCell(BaseModel):
    status: str                      # 'filled' | 'missing' | 'absent'
    value: str | None = None
    field_id: str | None = None      # the entity to jump to when filled
    inherited: bool = False          # filled from the notice-level value


class KeyLot(BaseModel):
    lot_index: str
    extracted: bool = True           # False: counted at classification, never emitted
    cells: dict[str, KeyCell]


class KeyChecklist(BaseModel):
    """Per-lot presence of the seven key entities (pipeline/key_entities.py)."""
    lots: list[KeyLot] = []
    total: int = 0
    filled: int = 0
    absent: int = 0
    missing: int = 0
    score: int = 0
    missing_labels: list[str] = []


class ExtractionReviewOut(BaseModel):
    filename: str
    markdown: str | None = None
    status: str = "pending"
    # Label-free quality score (0-100, pipeline/validators.py). None for
    # extractions written before scoring was tracked.
    score: int | None = None
    verified_by: str | None = None
    verified_at: str | None = None
    review_notes: str | None = None
    # The reviewer's lot count from the classification gate; drives the
    # un-extracted rows in `keys`.
    expected_lot_count: int | None = None
    # The lot × key checklist this stage is about. None for a stitched follower.
    keys: KeyChecklist | None = None
    # Source-notice location so the review UI can show the original document
    # next to the markdown (Document props set by scripts/upload_downloads_to_r2).
    public_url: str | None = None
    doc_type: str | None = None  # "image" | "pdf" | "other"
    content_type: str | None = None
    # True when the markdown was re-ingested AFTER this extraction ran, so the
    # stored fields (and their char offsets) no longer match the source text —
    # the reviewer should re-run LangExtract (the ▶ button, or
    # load_extractions --filename --force from a shell).
    stale: bool = False
    # A POST /rerun is in flight for this document; poll the detail endpoint
    # until this clears, then re-render. rerun_error carries the last rerun's
    # failure so the reviewer sees why nothing changed.
    rerun_running: bool = False
    rerun_error: str | None = None
    # Stitched notices (pipeline/notice_pages). A follower — page 2 of a
    # two-file notice — carries only stitched_into and no fields: its text and
    # lots are reviewed on the leader. A leader lists its pages and where each
    # starts in `markdown`, so the source pane can draw a divider.
    stitched_into: str | None = None
    stitched_pages: list[str] = []
    stitched_page_offsets: list[int] = []
    fields: list[ExtractionField] = []


class ExtractionQueueRow(BaseModel):
    filename: str
    status: str
    n_fields: int
    n_ungrounded: int
    # Label-free quality score (0-100, pipeline/validators.py). None for rows
    # extracted before scoring was tracked.
    score: int | None = None
    # When load_extractions.py last wrote this document's extraction (ISO string,
    # None for rows extracted before this was tracked). Lets the review UI sort
    # newest-first so a freshly re-run batch clusters at the top of the queue.
    extraction_at: str | None = None
    # Which extraction run produced this document — every doc from one
    # load_extractions.py invocation shares this number (rendered "B7" in the UI).
    # None for rows extracted before batches were tracked.
    extraction_batch: int | None = None
    # Markdown re-ingested after this extraction ran -> a re-run is required.
    stale: bool = False
    # Reviewer's lot count from the classification gate (Document.
    # expected_lot_count) vs distinct lot_index values in this extraction.
    # mismatch=True is the checksum firing: LangExtract missed lots or
    # invented extras. Either count None -> mismatch stays False (no claim).
    expected_lot_count: int | None = None
    extracted_lot_count: int | None = None
    lot_count_mismatch: bool = False
    # How many source files this row's text was stitched from (1 = a plain
    # single-file notice).
    stitched_pages: int = 1
    # Key-entity checklist summary (pipeline/key_entities.py): share of per-lot
    # key cells filled or marked absent, how many are still missing, and their
    # labels ("lot 2: extent") so the card can say what to fix before opening.
    key_score: int | None = None
    key_missing: int | None = None
    key_missing_labels: list[str] = []
    # Failure-filter keys this row carries (EXTRACTION_FAILURES, bar order), so
    # the card names what is wrong before the reviewer opens it.
    failures: list[str] = []


class ExtractionQueueOut(BaseModel):
    rows: list[ExtractionQueueRow] = []
    total: int = 0


class FieldEditBody(BaseModel):
    field_id: str = Field(min_length=1, max_length=200)
    value: str = Field(max_length=20000)
    notes: str | None = Field(default=None, max_length=2000)


class ExtractionVerifyBody(BaseModel):
    notes: str | None = Field(default=None, max_length=2000)


class AddFieldBody(BaseModel):
    """A field the model missed, added by the reviewer. start/end are char
    offsets into the document's current markdown when the reviewer selected
    the text there (grounded); omitted when typed by hand (ungrounded)."""
    cls: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=20000)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    lot_index: str | None = Field(default=None, max_length=20)
    attrs: dict[str, str | int | float | None] = {}


class KeyAbsentBody(BaseModel):
    lot_index: str = Field(min_length=1, max_length=20)
    key: str = Field(min_length=1, max_length=40)
    absent: bool = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def extraction_stale(md_reextracted_at: str | None, md_loaded_at: str | None,
                     extraction_at: str | None,
                     extraction_stale_at: str | None = None) -> bool:
    """True when the stored extraction no longer matches its inputs.

    Two signals. The markdown changed after LangExtract last ran on it —
    re-ingest stamps one of two markers depending on the path (a full MinerU
    re-ingest sets ``markdown_loaded_at``, a single-block re-OCR sets
    ``markdown_reextracted_at``). Or something upstream said so explicitly:
    ``extraction_stale_at`` is stamped when the classification (lot count or
    notice type) changes, when a missing region is recovered, or when pages
    are stitched — the prompt or the text the extraction came from is gone.
    Stale when ANY of the three is newer than ``extraction_at``. All are Neo4j
    datetimes rendered as ISO-8601 UTC strings, which compare correctly
    lexicographically. Unknown extraction time (legacy rows) -> not stale."""
    if not extraction_at:
        return False
    return any(t and t > extraction_at
               for t in (md_reextracted_at, md_loaded_at, extraction_stale_at))


# ── query helpers (kept here so queries.py is untouched) ──────────────────────
def get_extraction(filename: str) -> dict | None:
    rows = run_read_query(
        """
        MATCH (d:Document {filename: $fn})
        WHERE d.extraction_json IS NOT NULL
        RETURN d.filename                                   AS filename,
               coalesce(d.stitched_markdown, d.markdown)    AS markdown,
               d.extraction_json                            AS extraction_json,
               coalesce(d.extraction_corrections_json, '{}') AS corrections_json,
               coalesce(d.extraction_review_status, 'pending') AS status,
               d.extraction_score                           AS score,
               d.extraction_verified_by                     AS verified_by,
               toString(d.extraction_verified_at)           AS verified_at,
               d.extraction_review_notes                    AS review_notes,
               coalesce(d.stitched_expected_lot_count, d.expected_lot_count)
                                                            AS expected_lot_count,
               d.public_url                                 AS public_url,
               d.doc_type                                   AS doc_type,
               d.content_type                               AS content_type,
               toString(d.extraction_at)                    AS extraction_at,
               toString(d.markdown_reextracted_at)          AS markdown_reextracted_at,
               toString(d.markdown_loaded_at)               AS markdown_loaded_at,
               toString(d.extraction_stale_at)              AS extraction_stale_at,
               d.stitched_into                              AS stitched_into,
               coalesce(d.stitched_pages, [])                AS stitched_pages,
               coalesce(d.stitched_page_offsets, [])          AS stitched_page_offsets
        LIMIT 1
        """,
        {"fn": filename},
    )
    return rows[0] if rows else None


def get_stitch_pointer(filename: str) -> str | None:
    """The leader a follower page was stitched into, or None."""
    rows = run_read_query(
        "MATCH (d:Document {filename: $fn}) RETURN d.stitched_into AS leader",
        {"fn": filename})
    return (rows[0].get("leader") if rows else None) or None


# ── failure filters ───────────────────────────────────────────────────────────
# The extraction stage's answer to the markdown stage's OCR-failure pills: one
# pill per kind of miss, so a reviewer can work a single failure across the
# corpus ("every notice that lost lots") instead of reading notices top to
# bottom. Selected pills OR together, like the markdown flags.
#
# ``codes`` are pipeline/validators.py issue codes, stamped per document as
# ``extraction_issue_codes`` by pipeline/key_entities.stamp_key_scores (with
# reviewer corrections applied, so a gap the reviewer filled drops out).
# ``cypher`` is a live predicate for failures that depend on state the
# validator never sees — the reviewer's lot count, re-ingest timestamps — and
# so must be read at query time rather than stamped.
_EXPECTED_LOTS = "coalesce(d.stitched_expected_lot_count, d.expected_lot_count)"
_STALE_CYPHER = (
    "(d.extraction_at IS NOT NULL AND ("
    "coalesce(d.markdown_reextracted_at > d.extraction_at, false)"
    " OR coalesce(d.markdown_loaded_at > d.extraction_at, false)"
    " OR coalesce(d.extraction_stale_at > d.extraction_at, false)))")
EXTRACTION_FAILURES: dict[str, dict] = {
    # Only the reviewer's own lot count, never validators.py lot_under_recall:
    # that heuristic counts "S.No" markers, which survey numbers also carry,
    # and fired on ~30% of the corpus — nearly all notices with every lot.
    "missing-lots":  {"codes": (),
                      "cypher": f"coalesce(d.extraction_lot_count < {_EXPECTED_LOTS}, false)"},
    "extra-lots":    {"codes": (),
                      "cypher": f"coalesce(d.extraction_lot_count > {_EXPECTED_LOTS}, false)"},
    "rerun":         {"codes": (), "cypher": _STALE_CYPHER},
    "description":   {"codes": ("missing_full_description", "full_description_incomplete")},
    "reserve":       {"codes": ("missing_reserve_price", "lot_missing_reserve")},
    "borrower":      {"codes": ("missing_borrower", "lot_missing_borrower")},
    "location":      {"codes": ("missing_location", "lot_missing_location")},
    "extent":        {"codes": ("missing_extent",)},
    "property-type": {"codes": ("missing_property_type",)},
    "uds":           {"codes": ("missing_uds", "uds_parent_as_own_area")},
    "creditor":      {"codes": ("missing_secured_creditor",)},
    "ungrounded":    {"codes": ("ungrounded",)},
    "odd-values":    {"codes": ("reserve_out_of_range", "emd_ratio_off",
                                "possession_type_invalid", "kind_invalid")},
}


def _failure_codes(failures: list[str] | None) -> list[str]:
    """The validator codes the selected failure pills match on."""
    return sorted({c for f in (failures or [])
                   for c in EXTRACTION_FAILURES.get(f, {}).get("codes", ())})


def _failures_clause(failures: list[str] | None) -> str:
    """One OR'd predicate over the selected failures ('' when none)."""
    if not failures:
        return ""
    preds = [EXTRACTION_FAILURES[f]["cypher"] for f in failures
             if EXTRACTION_FAILURES.get(f, {}).get("cypher")]
    if _failure_codes(failures):
        preds.append("any(c IN coalesce(d.extraction_issue_codes, []) "
                     "WHERE c IN $fail_codes)")
    return "(" + " OR ".join(preds) + ")" if preds else ""


def row_failures(issue_codes, extracted_lots: int | None,
                 expected_lots: int | None, stale: bool) -> list[str]:
    """The failure keys one queue row carries, in filter-bar order — the same
    tests ``_failures_clause`` runs in Cypher, so a card always shows the pill
    that brought it into the filtered list."""
    codes = set(issue_codes or [])
    out = []
    for key, spec in EXTRACTION_FAILURES.items():
        hit = bool(codes & set(spec.get("codes", ())))
        if key == "missing-lots" and extracted_lots is not None and expected_lots is not None:
            hit = extracted_lots < expected_lots
        elif key == "extra-lots" and extracted_lots is not None and expected_lots is not None:
            hit = extracted_lots > expected_lots
        elif key == "rerun":
            hit = stale
        if hit:
            out.append(key)
    return out


def _extraction_filter_clause(
    status: str | None,
    score_min: float | None,
    score_max: float | None,
    notice_type: str | None,
    date_from: str | None,
    date_to: str | None,
    q: str | None,
    missing_keys: bool = False,
    failures: list[str] | None = None,
) -> str:
    """Shared WHERE tail for the extraction queue and its stats.

    Keeping list + stats on one clause builder stops the header pills from
    counting a different set than the queue shows — the same trap
    ``_classification_where`` exists to avoid on the other stages. The
    notice_type / date helpers are imported from ``queries`` rather than
    re-written so every stage filters a notice identically.
    """
    # A follower (page 2 of a stitched notice) is reviewed on its leader.
    clause = "AND d.stitched_into IS NULL"
    clause += " AND coalesce(d.extraction_review_status,'pending') = $status" if status else ""
    # Rows without a score (pre-scoring extractions) are excluded once either
    # bound narrows the default 0-100 range — nothing to compare against.
    if score_min is not None:
        clause += " AND d.extraction_score >= $score_min"
    if score_max is not None:
        clause += " AND d.extraction_score <= $score_max"
    # Rows never stamped with a key count (pre-checklist extractions) are
    # kept: unknown is not "complete", and the backfill closes the gap.
    if missing_keys:
        clause += " AND coalesce(d.extraction_key_missing, 1) > 0"
    fc = _failures_clause(failures)
    if fc:
        clause += f" AND {fc}"
    nt = _notice_type_clause(notice_type, alias="d")
    if nt:
        clause += f" AND {nt}"
    dt = _date_exists_clause(date_from, date_to, alias="d")
    if dt:
        clause += f" AND {dt}"
    if q:
        # Filename OR any linked listing's title/borrower, so the one search box
        # works whether the reviewer remembers the file or the property.
        clause += (
            " AND (toLower(coalesce(d.filename, '')) CONTAINS toLower($q)"
            " OR EXISTS { MATCH (d)<-[:HAS_DOCUMENT]-(_p:AuctionProperty)"
            "   WHERE toLower(coalesce(_p.title, '')) CONTAINS toLower($q) }"
            " OR EXISTS { MATCH (d)<-[:HAS_DOCUMENT]-(:AuctionProperty)"
            "   -[:HAS_BORROWER]->(_b:Borrower)"
            "   WHERE toLower(coalesce(_b.name, '')) CONTAINS toLower($q) })"
        )
    return clause


def list_extraction_queue(status: str | None, limit: int, sort: str = "recent",
                          score_min: float | None = None,
                          score_max: float | None = None,
                          notice_type: str | None = None,
                          date_from: str | None = None,
                          date_to: str | None = None,
                          q: str | None = None,
                          missing_keys: bool = False,
                          failures: list[str] | None = None) -> list[dict]:
    clause = _extraction_filter_clause(status, score_min, score_max,
                                       notice_type, date_from, date_to, q,
                                       missing_keys=missing_keys,
                                       failures=failures)
    # "recent" (default): latest batch first (then newest extraction within it) so
    # a just-run batch groups at the top; docs missing extraction data (extracted
    # before it was tracked) fall to the bottom. "name": alphabetical by filename.
    # "keys": most key entities missing first (lowest key score), the order a
    # reviewer works the checklist in; unstamped rows sort as worst-case.
    if sort == "name":
        order = "d.filename"
    elif sort == "keys":
        order = ("coalesce(d.extraction_key_score, -1) ASC, "
                 "coalesce(d.extraction_key_missing, 999) DESC, "
                 "d.extraction_at DESC, d.filename")
    else:
        order = ("d.extraction_at IS NULL, coalesce(d.extraction_batch,-1) DESC, "
                 "d.extraction_at DESC, d.filename")
    return run_read_query(
        f"""
        MATCH (d:Document)
        WHERE d.extraction_json IS NOT NULL {clause}
        RETURN d.filename AS filename,
               coalesce(d.extraction_review_status,'pending') AS status,
               d.extraction_score AS score,
               toString(d.extraction_at) AS extraction_at,
               toString(d.markdown_reextracted_at) AS markdown_reextracted_at,
               toString(d.markdown_loaded_at) AS markdown_loaded_at,
               d.extraction_batch AS extraction_batch,
               toString(d.extraction_stale_at) AS extraction_stale_at,
               coalesce(d.stitched_expected_lot_count, d.expected_lot_count) AS expected_lot_count,
               coalesce(d.stitched_pages, [d.filename]) AS stitched_pages,
               d.extraction_json AS extraction_json,
               coalesce(d.extraction_corrections_json, '{{}}') AS corrections_json,
               d.extraction_issue_codes AS issue_codes
        ORDER BY {order}
        LIMIT $limit
        """,
        {"status": status, "limit": limit,
         "score_min": score_min, "score_max": score_max,
         "date_from": date_from, "date_to": date_to, "q": q,
         "fail_codes": _failure_codes(failures)},
        max_rows=5000,
    )


def count_extraction_queue(status: str | None,
                           score_min: float | None = None,
                           score_max: float | None = None,
                           notice_type: str | None = None,
                           date_from: str | None = None,
                           date_to: str | None = None,
                           q: str | None = None,
                           missing_keys: bool = False,
                           failures: list[str] | None = None) -> int:
    """How many documents match the queue filters, ignoring the row limit."""
    clause = _extraction_filter_clause(status, score_min, score_max,
                                       notice_type, date_from, date_to, q,
                                       missing_keys=missing_keys,
                                       failures=failures)
    rows = run_read_query(
        f"MATCH (d:Document) WHERE d.extraction_json IS NOT NULL {clause} "
        "RETURN count(d) AS n",
        {"status": status, "score_min": score_min, "score_max": score_max,
         "date_from": date_from, "date_to": date_to, "q": q,
         "fail_codes": _failure_codes(failures)},
        max_rows=1, timeout=30.0)
    return int(rows[0]["n"]) if rows else 0


def bulk_verify_extractions(by_email: str,
                            score_min: float | None = None,
                            score_max: float | None = None,
                            notice_type: str | None = None,
                            date_from: str | None = None,
                            date_to: str | None = None,
                            q: str | None = None,
                            dry_run: bool = False,
                            failures: list[str] | None = None) -> dict:
    """Mark every PENDING extraction matching the reviewer's current filters as
    verified.

    Shares ``_extraction_filter_clause`` with the queue and the stats, so the
    "Confirm all N" button acts on exactly the set it counted. Status is pinned
    to 'pending' rather than taken from the caller: re-verifying something
    already verified is a no-op, and sweeping 'edited' rows back to 'verified'
    would silently discard the fact that a human changed fields there.

    The intended use is with the score filter — confirm the high-scoring tail in
    one action and spend review time on the low scores.
    """
    clause = _extraction_filter_clause("pending", score_min, score_max,
                                       notice_type, date_from, date_to, q,
                                       failures=failures)
    params = {"status": "pending", "score_min": score_min, "score_max": score_max,
              "date_from": date_from, "date_to": date_to, "q": q, "by": by_email,
              "fail_codes": _failure_codes(failures)}
    if dry_run:
        rows = run_read_query(
            f"MATCH (d:Document) WHERE d.extraction_json IS NOT NULL {clause} "
            "RETURN count(d) AS n",
            params, max_rows=1, timeout=30.0)
        return {"count": int(rows[0]["n"]) if rows else 0, "dry_run": True}
    rows = run_query(
        f"""
        MATCH (d:Document)
        WHERE d.extraction_json IS NOT NULL {clause}
        SET d.extraction_review_status = 'verified',
            d.extraction_verified_by   = $by,
            d.extraction_verified_at   = datetime()
        RETURN count(d) AS n
        """,
        params)
    return {"count": int(rows[0]["n"]) if rows else 0, "dry_run": False}


def extraction_stats(score_min: float | None = None,
                     score_max: float | None = None,
                     notice_type: str | None = None,
                     date_from: str | None = None,
                     date_to: str | None = None,
                     q: str | None = None,
                     failures: list[str] | None = None) -> dict:
    """Header pill counts for the extraction stage, under the reviewer's
    current filters (status is deliberately NOT applied — the pills ARE the
    status breakdown)."""
    clause = _extraction_filter_clause(None, score_min, score_max,
                                       notice_type, date_from, date_to, q,
                                       failures=failures)
    rows = run_read_query(
        f"""
        MATCH (d:Document)
        WHERE d.extraction_json IS NOT NULL {clause}
        WITH coalesce(d.extraction_review_status,'pending') AS st,
             coalesce(d.extraction_key_missing, 1) AS km
        RETURN count(*) AS total,
               sum(CASE WHEN st = 'pending'  THEN 1 ELSE 0 END) AS pending,
               sum(CASE WHEN st = 'verified' THEN 1 ELSE 0 END) AS verified,
               sum(CASE WHEN st = 'edited'   THEN 1 ELSE 0 END) AS edited,
               sum(CASE WHEN km > 0          THEN 1 ELSE 0 END) AS missing_keys
        """,
        {"score_min": score_min, "score_max": score_max,
         "date_from": date_from, "date_to": date_to, "q": q,
         "fail_codes": _failure_codes(failures)},
        max_rows=1, timeout=30.0,
    )
    r = rows[0] if rows else {}
    return {
        "total":    int(r.get("total") or 0),
        "pending":  int(r.get("pending") or 0),
        "verified": int(r.get("verified") or 0),
        "edited":   int(r.get("edited") or 0),
        # Documents with at least one key cell still missing (unstamped rows
        # count: unknown is not complete) — see pipeline/key_entities.py.
        "missing_keys": int(r.get("missing_keys") or 0),
    }


def _load_corrections(filename: str) -> dict | None:
    """The document's corrections dict, or None when it has no extraction."""
    cur = run_read_query(
        "MATCH (d:Document {filename:$fn}) WHERE d.extraction_json IS NOT NULL "
        "RETURN coalesce(d.extraction_corrections_json,'{}') AS c LIMIT 1",
        {"fn": filename})
    if not cur:
        return None
    try:
        corr = json.loads(cur[0]["c"] or "{}")
    except json.JSONDecodeError:
        corr = {}
    return corr if isinstance(corr, dict) else {}


def _write_corrections(filename: str, corr: dict) -> bool:
    """Persist the corrections dict, mark the document edited, and restamp the
    key score — every reviewer write changes what the checklist counts, and
    the queue sorts on the stored number, so the two must move together."""
    rows = run_query(
        """
        MATCH (d:Document {filename:$fn})
        WHERE d.extraction_json IS NOT NULL
        SET d.extraction_corrections_json = $c,
            d.extraction_review_status    = 'edited'
        RETURN d.filename AS filename
        """,
        {"fn": filename, "c": json.dumps(corr, ensure_ascii=False)})
    if not rows:
        return False
    stamp_key_scores([filename])
    return True


def save_field_correction(filename: str, field_id: str, value: str,
                          by_email: str, notes: str | None) -> bool:
    corr = _load_corrections(filename)
    if corr is None:
        return False
    if field_id.startswith(ADDED_PREFIX):
        # Correcting a reviewer-added entity edits it in place — it has no
        # model text to keep a correction beside.
        cur = corr.get(field_id)
        if not isinstance(cur, dict):
            return False
        cur.update(text=value, by=by_email, at=_now())
        # The typed text no longer matches the selected span.
        cur["start"] = cur["end"] = None
    else:
        corr[field_id] = {"value": value, "by": by_email, "at": _now(), "notes": notes}
    return _write_corrections(filename, corr)


def add_field(filename: str, cls: str, text: str, start: int | None,
              end: int | None, lot_index: str | None, attrs: dict,
              by_email: str) -> str | None:
    """Store a reviewer-added entity; returns its field id (``add:<id>``)."""
    corr = _load_corrections(filename)
    if corr is None:
        return None
    fid = ADDED_PREFIX + uuid.uuid4().hex[:8]
    a = {k: v for k, v in (attrs or {}).items() if v not in (None, "")}
    if lot_index:
        a["lot_index"] = str(lot_index)
    grounded = isinstance(start, int) and isinstance(end, int) and start < end
    corr[fid] = {"cls": cls, "text": text.strip(),
                 "start": start if grounded else None,
                 "end": end if grounded else None,
                 "attrs": a, "by": by_email, "at": _now()}
    return fid if _write_corrections(filename, corr) else None


def remove_added_field(filename: str, field_id: str) -> bool:
    corr = _load_corrections(filename)
    if corr is None or field_id not in corr:
        return False
    del corr[field_id]
    return _write_corrections(filename, corr)


def set_key_absent(filename: str, lot_index: str, key: str, absent: bool,
                   by_email: str) -> bool:
    corr = _load_corrections(filename)
    if corr is None:
        return False
    k = absent_key(lot_index, key)
    if absent:
        corr[k] = {"by": by_email, "at": _now()}
    else:
        corr.pop(k, None)
    return _write_corrections(filename, corr)


def verify_extraction(filename: str, by_email: str, notes: str | None) -> bool:
    rows = run_query(
        """
        MATCH (d:Document {filename:$fn})
        SET d.extraction_review_status = 'verified',
            d.extraction_verified_by   = $by,
            d.extraction_verified_at   = datetime(),
            d.extraction_review_notes  = CASE
                WHEN $notes IS NULL OR $notes = ''
                THEN d.extraction_review_notes
                ELSE $notes END
        RETURN d.filename AS filename
        """,
        {"fn": filename, "by": by_email, "notes": notes})
    return bool(rows)


def unverify_extraction(filename: str) -> bool:
    rows = run_query(
        """
        MATCH (d:Document {filename:$fn})
        SET d.extraction_review_status = 'edited'
        REMOVE d.extraction_verified_by, d.extraction_verified_at
        RETURN d.filename AS filename
        """,
        {"fn": filename})
    return bool(rows)


# ── shaping ──────────────────────────────────────────────────────────────────
def count_extracted_lots(ents: list[dict]) -> int | None:
    """Distinct lots in one extraction, from entity ``lot_index`` attributes.

    Multi-notice extractions stamp every entity with its lot's index
    (pipeline/langextract_examples.py); single-notice extractions usually
    carry none, so any entities without a single lot_index count as 1 lot.
    Returns None for an empty extraction — no entities is "no claim", not
    "zero lots".
    """
    if not ents:
        return None
    idxs = set()
    for e in ents:
        attrs = e.get("attrs")
        if isinstance(attrs, dict):
            li = attrs.get("lot_index")
            if li not in (None, ""):
                idxs.add(str(li))
    return len(idxs) if idxs else 1


def _build_fields(extraction_json: str, corrections_json: str,
                  markdown: str | None = None,
                  markdown_changed: bool = False) -> list[ExtractionField]:
    """Shape stored entities into review fields, re-anchored against ``markdown``.

    ``markdown`` is the document's CURRENT text. The stored offsets were taken
    against whatever it was when langextract ran, so they are re-verified here
    and re-found when they no longer land (api/review/grounding.py). Passing
    None keeps the stored spans untouched — see ``reanchor``.

    ``markdown_changed`` is this document's ``stale`` verdict — the markdown was
    rewritten after the extraction ran. It is the difference between "this span
    is approximate" (keep it) and "this span describes a string that no longer
    exists" (drop it), so the two must be read from the same source; see the
    grounding module docstring.
    """
    try:
        ents = json.loads(extraction_json or "[]")
    except json.JSONDecodeError:
        ents = []
    try:
        corr = json.loads(corrections_json or "{}")
    except json.JSONDecodeError:
        corr = {}
    if not isinstance(ents, list):
        ents = []
    # Reviewer-added entities ride along after the model's own, and are
    # re-anchored with them: their offsets point into the same markdown.
    ents = ents + added_entities(corr)
    ents, _ = reanchor(ents, markdown, markdown_changed=markdown_changed)
    out: list[ExtractionField] = []
    for i, e in enumerate(ents):
        fid = e.get("id") or str(i)
        c = {} if e.get("added") else (corr.get(fid) or {})
        attrs = e.get("attrs") or {}
        out.append(ExtractionField(
            id=fid, cls=e.get("cls", ""), text=e.get("text", ""),
            start=e.get("start"), end=e.get("end"),
            # Grounded means "we can point at it in the text on screen now",
            # not "the extractor once returned an offset" — a lost anchor has
            # to read as ungrounded or the UI keeps promising evidence it can
            # no longer show.
            grounded=e.get("start") is not None,
            anchor=e.get("anchor", ANCHOR_STORED),
            lot_index=attrs.get("lot_index"),
            attrs={k: v for k, v in attrs.items() if k != "lot_index"},
            corrected_value=c.get("value"), corrected_by=c.get("by"),
            corrected_at=c.get("at"),
            added=bool(e.get("added")),
        ))
    return out


# ── single-document LangExtract rerun ────────────────────────────────────────
# In-process job registry: one rerun per document at a time. Entries live only
# for the server process — a restart forgets a finished error, which is fine;
# the graph's extraction_at is the durable record of success.
_RERUNS: dict[str, dict] = {}
_RERUNS_LOCK = threading.Lock()


def _rerun_state(filename: str) -> tuple[bool, str | None]:
    with _RERUNS_LOCK:
        st = _RERUNS.get(filename)
        if not st:
            return False, None
        return st["status"] == "running", st.get("error")


def _rerun_worker(filename: str) -> None:
    """Run the canonical single-document LangExtract path (same code the batch
    scripts use: per-notice-type model routing, validators score, load_extractions
    write shape). Any failure — including langextract not being installed on
    this server — lands in the registry for the UI to surface."""
    try:
        rows = run_read_query(
            "MATCH (d:Document {filename: $fn}) "
            "RETURN d.filename AS filename, "
            "       coalesce(d.stitched_markdown, d.markdown) AS md, "
            "       d.notice_type AS notice_type, "
            "       coalesce(d.stitched_expected_lot_count, d.expected_lot_count) "
            "AS expected_lot_count, "
            "       d.stitched_into AS stitched_into",
            {"fn": filename})
        if rows and rows[0].get("stitched_into"):
            raise RuntimeError(f"this page is stitched into {rows[0]['stitched_into']}; "
                               "re-run that document instead")
        if not rows or not (rows[0].get("md") or "").strip():
            raise RuntimeError("document has no markdown to extract from")
        from pipeline.load_extractions import _next_batch
        from scripts.reset_langextract_and_extract import _extract_one
        _extract_one(rows[0], _next_batch(), route=True)
        with _RERUNS_LOCK:
            _RERUNS.pop(filename, None)
    except Exception as e:  # surfaced via rerun_error, never crashes the app
        with _RERUNS_LOCK:
            _RERUNS[filename] = {"status": "error", "error": f"{type(e).__name__}: {e}"}


# ── endpoints ────────────────────────────────────────────────────────────────
def _no_all(v) -> str | None:
    """Normalise an optional filter value to a real string or None.

    The shared filter bar sends 'all' to mean "no filter"; the query layer
    expects None, and blank strings collapse the same way. Anything that is not
    a string also becomes None — calling an endpoint function directly (as the
    tests do) leaves FastAPI's ``Query(default=None)`` sentinel in place, and
    passing that object down reaches `_notice_type_clause` as an unknown filter.
    """
    if not isinstance(v, str):
        return None
    v = v.strip()
    return None if v in ("", "all") else v


def _clean_failures(failures) -> list[str] | None:
    """Keep only failure keys the filter bar knows, in its order. An unknown
    key is a 422 rather than silently ignored — ignoring it would widen the
    list to everything, the opposite of what the reviewer asked for."""
    if not isinstance(failures, list) or not failures:
        return None
    unknown = sorted({f for f in failures if f not in EXTRACTION_FAILURES})
    if unknown:
        raise HTTPException(status_code=422,
                            detail=f"unknown extraction failure(s): {unknown}")
    return [f for f in EXTRACTION_FAILURES if f in set(failures)]


class ExtractionStats(BaseModel):
    total: int
    pending: int
    verified: int
    edited: int
    missing_keys: int = 0


# NOTE: declared before the `/{filename:path}` catch-all below, or that route
# swallows it and /stats resolves as a filename.
@router.get("/stats", response_model=ExtractionStats)
def extraction_stats_endpoint(
    score_min: float | None = Query(default=None, ge=0.0, le=100.0),
    score_max: float | None = Query(default=None, ge=0.0, le=100.0),
    notice_type: str | None = Query(default=None),
    date_from: str | None = Query(default=None, max_length=20),
    date_to: str | None = Query(default=None, max_length=20),
    q: str | None = Query(default=None, max_length=200),
    # Repeatable: ?failures=missing-lots&failures=rerun (OR), as on /queue.
    failures: list[str] | None = Query(default=None),
    _admin: UserOut = Depends(get_current_admin),
) -> ExtractionStats:
    return ExtractionStats(**extraction_stats(
        score_min=score_min, score_max=score_max,
        notice_type=_no_all(notice_type),
        date_from=_no_all(date_from), date_to=_no_all(date_to), q=_no_all(q),
        failures=_clean_failures(failures),
    ))


class ExtractionBulkConfirmBody(BaseModel):
    score_min: float | None = Field(default=None, ge=0.0, le=100.0)
    score_max: float | None = Field(default=None, ge=0.0, le=100.0)
    notice_type: str | None = Field(default=None, max_length=20)
    date_from: str | None = Field(default=None, max_length=20)
    date_to: str | None = Field(default=None, max_length=20)
    q: str | None = Field(default=None, max_length=200)
    # The failure pills the reviewer has on: "Confirm all N" must act on the
    # same filtered set the N was counted over.
    failures: list[str] | None = None
    dry_run: bool = False


class ExtractionBulkConfirmResult(BaseModel):
    count: int
    dry_run: bool


# Declared before the `/{filename:path}` catch-all, like /stats.
@router.post("/bulk-confirm", response_model=ExtractionBulkConfirmResult)
def extraction_bulk_confirm(
    body: ExtractionBulkConfirmBody,
    admin: UserOut = Depends(get_current_admin),
) -> ExtractionBulkConfirmResult:
    return ExtractionBulkConfirmResult(**bulk_verify_extractions(
        by_email=admin.email,
        score_min=body.score_min, score_max=body.score_max,
        notice_type=_no_all(body.notice_type),
        date_from=body.date_from, date_to=body.date_to, q=body.q,
        dry_run=body.dry_run, failures=_clean_failures(body.failures),
    ))


@router.get("/queue", response_model=ExtractionQueueOut)
def extraction_queue(
    status: str | None = Query(default=None),
    limit: int = Query(default=200, le=2000),
    sort: str = Query(default="recent", pattern="^(recent|name|keys)$"),
    score_min: float | None = Query(default=None, ge=0.0, le=100.0),
    score_max: float | None = Query(default=None, ge=0.0, le=100.0),
    notice_type: str | None = Query(default=None),
    date_from: str | None = Query(default=None, max_length=20),
    date_to: str | None = Query(default=None, max_length=20),
    q: str | None = Query(default=None, max_length=200),
    missing_keys: bool = Query(default=False),
    # Repeatable: ?failures=missing-lots&failures=rerun — rows carrying ANY.
    failures: list[str] | None = Query(default=None),
    _admin: UserOut = Depends(get_current_admin),
) -> ExtractionQueueOut:
    status, notice_type = _no_all(status), _no_all(notice_type)
    date_from, date_to, q = _no_all(date_from), _no_all(date_to), _no_all(q)
    missing_keys = missing_keys is True
    failures = _clean_failures(failures)
    rows = list_extraction_queue(status, limit, sort,
                                 score_min=score_min, score_max=score_max,
                                 notice_type=notice_type,
                                 date_from=date_from, date_to=date_to, q=q,
                                 missing_keys=missing_keys, failures=failures)
    out = []
    for r in rows:
        try:
            ents = json.loads(r["extraction_json"] or "[]")
        except json.JSONDecodeError:
            ents = []
        b = r.get("extraction_batch")
        s = r.get("score")
        elc = r.get("expected_lot_count")
        expected = int(elc) if elc is not None else None
        extracted = count_extracted_lots(ents)
        # Computed live rather than read from the stored stamp: the stamp
        # exists for ORDER BY, the labels for the reviewer, and the two must
        # not disagree on a row the reviewer just edited.
        keys = key_checklist_from_stored(r.get("extraction_json"),
                                         r.get("corrections_json"), expected)
        stale = extraction_stale(r.get("markdown_reextracted_at"),
                                 r.get("markdown_loaded_at"),
                                 r.get("extraction_at"),
                                 r.get("extraction_stale_at"))
        out.append(ExtractionQueueRow(
            filename=r["filename"], status=r["status"], n_fields=len(ents),
            # As-extracted, deliberately NOT re-anchored: this is a list query,
            # and re-anchoring means holding every row's markdown in memory to
            # answer one integer. The count the reviewer acts on is the
            # detail view's, which is re-anchored; the queue's signal that the
            # two can disagree is `stale` on the same row.
            n_ungrounded=sum(1 for e in ents if e.get("start") is None),
            score=int(s) if s is not None else None,
            extraction_at=r.get("extraction_at"),
            extraction_batch=int(b) if b is not None else None,
            stale=stale,
            expected_lot_count=expected,
            extracted_lot_count=extracted,
            lot_count_mismatch=(expected is not None
                                and extracted is not None
                                and expected != extracted),
            stitched_pages=len(r.get("stitched_pages") or [r["filename"]]),
            key_score=keys["score"], key_missing=keys["missing"],
            key_missing_labels=keys["missing_labels"],
            failures=row_failures(r.get("issue_codes"), extracted, expected, stale)))
    # A genuine count, not len(out): the row list is capped by $limit, and the
    # "Confirm all N in range" button acts on the whole matching set — so a
    # capped total would understate what the button is about to verify.
    total = count_extraction_queue(status, score_min=score_min,
                                   score_max=score_max,
                                   notice_type=notice_type,
                                   date_from=date_from, date_to=date_to, q=q,
                                   missing_keys=missing_keys, failures=failures)
    return ExtractionQueueOut(rows=out, total=total)


@router.get("/{filename:path}", response_model=ExtractionReviewOut)
def extraction_detail(
    filename: str,
    _admin: UserOut = Depends(get_current_admin),
) -> ExtractionReviewOut:
    row = get_extraction(filename)
    # Page 2 of a stitched notice: whatever it holds is reviewed on the leader.
    leader = (row or {}).get("stitched_into") or (get_stitch_pointer(filename) if row is None else None)
    if leader:
        return ExtractionReviewOut(filename=filename, stitched_into=leader,
                                   status="pending", fields=[])
    if row is None:
        raise HTTPException(status_code=404, detail="extraction not found")
    running, error = _rerun_state(filename)
    stale = extraction_stale(row.get("markdown_reextracted_at"),
                             row.get("markdown_loaded_at"),
                             row.get("extraction_at"),
                             row.get("extraction_stale_at"))
    elc = row.get("expected_lot_count")
    expected = int(elc) if elc is not None else None
    keys = key_checklist_from_stored(row.get("extraction_json"),
                                     row.get("corrections_json"), expected)
    return ExtractionReviewOut(
        filename=row["filename"], markdown=row.get("markdown"),
        status=row.get("status", "pending"), score=row.get("score"),
        verified_by=row.get("verified_by"), verified_at=row.get("verified_at"),
        review_notes=row.get("review_notes"),
        expected_lot_count=expected,
        keys=KeyChecklist(**keys),
        public_url=row.get("public_url"), doc_type=row.get("doc_type"),
        content_type=row.get("content_type"),
        stale=stale,
        rerun_running=running, rerun_error=error,
        stitched_pages=list(row.get("stitched_pages") or []),
        stitched_page_offsets=[int(o) for o in (row.get("stitched_page_offsets") or [])],
        fields=_build_fields(row["extraction_json"], row["corrections_json"],
                             row.get("markdown"), stale),
    )


@router.post("/{filename:path}/field", response_model=ExtractionReviewOut)
def extraction_edit_field(
    filename: str,
    body: FieldEditBody,
    admin: UserOut = Depends(get_current_admin),
) -> ExtractionReviewOut:
    if get_stitch_pointer(filename):
        raise HTTPException(status_code=409,
                            detail="this page is stitched into another document; review that one")
    if not save_field_correction(filename, body.field_id, body.value,
                                 admin.email, body.notes):
        raise HTTPException(status_code=404, detail="extraction not found")
    return extraction_detail(filename, admin)


@router.post("/{filename:path}/add-field", response_model=ExtractionReviewOut)
def extraction_add_field(
    filename: str,
    body: AddFieldBody,
    admin: UserOut = Depends(get_current_admin),
) -> ExtractionReviewOut:
    """Add an entity the model missed — the reviewer's answer to a missing key
    cell. Grounded when start/end are given (the reviewer selected the text in
    the markdown pane); the text must then be exactly what those offsets
    cover, or the highlight would point at the wrong passage."""
    if get_stitch_pointer(filename):
        raise HTTPException(status_code=409,
                            detail="this page is stitched into another document; review that one")
    if body.cls not in ENTITY_CLASSES:
        raise HTTPException(status_code=422, detail=f"unknown extraction class {body.cls!r}")
    start, end = body.start, body.end
    if (start is None) != (end is None):
        raise HTTPException(status_code=422, detail="start and end go together")
    if start is not None:
        if end <= start:
            raise HTTPException(status_code=422, detail="end must be after start")
        row = get_extraction(filename)
        if row is None:
            raise HTTPException(status_code=404, detail="extraction not found")
        md = row.get("markdown") or ""
        if md[start:end] != body.text:
            raise HTTPException(status_code=422,
                                detail="text does not match the markdown at start:end")
    fid = add_field(filename, body.cls, body.text, start, end, body.lot_index,
                    dict(body.attrs), admin.email)
    if not fid:
        raise HTTPException(status_code=404, detail="extraction not found")
    return extraction_detail(filename, admin)


@router.delete("/{filename:path}/field/{field_id}", response_model=ExtractionReviewOut)
def extraction_delete_added_field(
    filename: str,
    field_id: str,
    admin: UserOut = Depends(get_current_admin),
) -> ExtractionReviewOut:
    """Remove a reviewer-added entity. Model entities cannot be deleted here —
    they are corrected, and the model's output stays on record."""
    if not field_id.startswith(ADDED_PREFIX):
        raise HTTPException(status_code=422, detail="only reviewer-added fields can be deleted")
    if get_stitch_pointer(filename):
        raise HTTPException(status_code=409,
                            detail="this page is stitched into another document; review that one")
    if not remove_added_field(filename, field_id):
        raise HTTPException(status_code=404, detail="added field not found")
    return extraction_detail(filename, admin)


@router.post("/{filename:path}/key-absent", response_model=ExtractionReviewOut)
def extraction_key_absent(
    filename: str,
    body: KeyAbsentBody,
    admin: UserOut = Depends(get_current_admin),
) -> ExtractionReviewOut:
    """Mark one lot's key entity as not stated in the notice (or clear that
    mark). Counted as done by the checklist without inventing a value."""
    if body.key not in KEYS:
        raise HTTPException(status_code=422, detail=f"unknown key {body.key!r}")
    if get_stitch_pointer(filename):
        raise HTTPException(status_code=409,
                            detail="this page is stitched into another document; review that one")
    if not set_key_absent(filename, body.lot_index, body.key, body.absent, admin.email):
        raise HTTPException(status_code=404, detail="extraction not found")
    return extraction_detail(filename, admin)


@router.post("/{filename:path}/rerun", response_model=ExtractionReviewOut)
def extraction_rerun(
    filename: str,
    admin: UserOut = Depends(get_current_admin),
) -> ExtractionReviewOut:
    """Re-run LangExtract for one document — the reviewer's follow-through
    after fixing its markdown (re-ingest / block re-OCR). Kicks off a
    background worker and returns immediately with rerun_running=true; the UI
    polls the detail endpoint until it clears. One rerun per document at a
    time; a second click while running is a 409."""
    if get_extraction(filename) is None:
        raise HTTPException(status_code=404, detail="extraction not found")
    if get_stitch_pointer(filename):
        raise HTTPException(status_code=409,
                            detail="this page is stitched into another document; re-run that one")
    with _RERUNS_LOCK:
        st = _RERUNS.get(filename)
        if st and st["status"] == "running":
            raise HTTPException(status_code=409, detail="rerun already running")
        _RERUNS[filename] = {"status": "running"}
    threading.Thread(target=_rerun_worker, args=(filename,),
                     daemon=True, name=f"rerun-{filename[:40]}").start()
    return extraction_detail(filename, admin)


@router.post("/{filename:path}/verify", response_model=ExtractionReviewOut)
def extraction_verify(
    filename: str,
    body: ExtractionVerifyBody,
    admin: UserOut = Depends(get_current_admin),
) -> ExtractionReviewOut:
    if get_stitch_pointer(filename):
        raise HTTPException(status_code=409,
                            detail="this page is stitched into another document; review that one")
    if not verify_extraction(filename, admin.email, body.notes):
        raise HTTPException(status_code=404, detail="extraction not found")
    return extraction_detail(filename, admin)


@router.post("/{filename:path}/unverify", response_model=ExtractionReviewOut)
def extraction_unverify(
    filename: str,
    admin: UserOut = Depends(get_current_admin),
) -> ExtractionReviewOut:
    if get_stitch_pointer(filename):
        raise HTTPException(status_code=409,
                            detail="this page is stitched into another document; review that one")
    if not unverify_extraction(filename):
        raise HTTPException(status_code=404, detail="extraction not found")
    return extraction_detail(filename, admin)
