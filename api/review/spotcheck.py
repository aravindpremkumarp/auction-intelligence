"""
api/review/spotcheck.py
-----------------------
Admin-only `/review/spotcheck/*` endpoints — the audit queue.

This is NOT the extraction review queue. The two answer different questions and
must not share state:

  /review/extraction/*  "make this document right"  — triage, biased on
                        purpose toward suspicious documents, writes
                        corrections back onto the Document.
  /review/spotcheck/*   "how right is the pipeline" — a seeded RANDOM sample
                        over a stated population, stored on its own node,
                        never written back to any Document.

Keeping the verdicts off the Document is the whole point. If an audit verdict
landed in ``extraction_corrections_json`` it would (a) change the population
the next audit draws from, and (b) flow into the eval gold set via
``evals/export_review_gold.py`` — turning the measuring stick into a mirror
that reports whatever the last audit said.

Data model — one node per audit round, nothing else touched:
  (:SpotCheckSample {
     id, created_at, created_by, seed, size,
     scope_json,     JSON of the filters the population was drawn under
     items_json,     JSON [{key, filename, field_id, cls, attr, value,
                            start, end, lot_index, stratum, question}]
     verdicts_json,  JSON {key: {verdict, note, by, at}}
     pool_documents, excluded_stale   — provenance for the draw
  })

``items_json`` is FROZEN at draw time. A sample that silently re-draws when the
corpus changes cannot be re-checked, and two people reviewing "the same sample"
would be reviewing different things.
"""
from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.auth.dependencies import get_current_admin
from api.auth.schemas import UserOut
from api.neo4j_client import run_query, run_read_query
from api.review.extraction import extraction_stale
from api.review.grounding import reanchor
from api.review.queries import _date_exists_clause, _notice_type_clause
from pipeline.spotcheck import (
    VERDICTS,
    build_report,
    describe_scope,
    draw_sample,
    filter_claims,
    iter_claims,
)

router = APIRouter(prefix="/review/spotcheck", tags=["review-spotcheck"])

# How many documents the draw pulls before enumerating claims. A two-stage
# sample (documents, then claims within them) rather than one pass over the
# whole corpus, because enumerating claims means parsing every extraction_json
# and a large corpus will not fit in one request. Both stages use the sample's
# seed, so the two-stage draw is still reproducible end to end.
_DEFAULT_POOL = 300
# Characters of markdown shown either side of the claim's span. Enough to judge
# a value in context without shipping a whole multi-lot notice per item.
_CONTEXT = 1200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── models ───────────────────────────────────────────────────────────────────
class SpotCheckScope(BaseModel):
    """The population a sample is drawn from. Stored verbatim with the sample."""
    # Constrained at the edge: `_notice_type_clause` raises ValueError on an
    # unknown filter, which would surface as a 500. A pattern here makes it a
    # 422 with the allowed values named.
    notice_type: str | None = Field(default=None,
                                    pattern="^(all|single|multi|unclassified)$")
    date_from: str | None = Field(default=None, max_length=20)
    date_to: str | None = Field(default=None, max_length=20)
    score_min: float | None = Field(default=None, ge=0.0, le=100.0)
    score_max: float | None = Field(default=None, ge=0.0, le=100.0)
    batch: int | None = None
    # Restrict the draw to these fields. Part of the SCOPE, not a draw option,
    # because a focused sample says nothing about the fields it left out — the
    # list has to travel with the report or "92% accurate" reads as a claim
    # about the whole extraction. Empty/omitted audits every field.
    #
    # A bare name matches both senses ("extent" -> cls:extent and attr:extent);
    # an explicit "attr:village" / "cls:borrower" narrows to one.
    fields: list[str] = Field(default_factory=list, max_length=60)


class SpotCheckCreateBody(SpotCheckScope):
    size: int = Field(default=100, ge=1, le=1000)
    # Explicit seed makes an audit re-drawable; omitted, one is generated and
    # stored, so it is always recoverable after the fact.
    seed: int | None = None
    pool_documents: int = Field(default=_DEFAULT_POOL, ge=1, le=2000)
    max_per_document: int | None = Field(default=8, ge=1)


class SpotCheckItem(BaseModel):
    key: str
    filename: str
    field_id: str
    cls: str
    attr: str | None = None
    value: str = ""
    span_text: str = ""
    question: str = ""
    stratum: str = ""
    start: int | None = None
    end: int | None = None
    lot_index: str | None = None
    verdict: str | None = None
    note: str | None = None


class SpotCheckItemDetail(SpotCheckItem):
    """One item plus everything needed to judge it."""
    # Markdown window around the span, with offsets relative to the window.
    context: str = ""
    context_start: int | None = None
    context_end: int | None = None
    public_url: str | None = None
    doc_type: str | None = None
    # The markdown moved since extraction, so the span may no longer point at
    # the value. Surfaced rather than hidden: judging a drifted span is how an
    # audit records a verdict about the wrong text.
    stale: bool = False
    anchor: str = "stored"
    index: int = 0
    total: int = 0


class SpotCheckSummary(BaseModel):
    id: str
    created_at: str | None = None
    created_by: str | None = None
    seed: int | None = None
    size: int = 0
    judged: int = 0
    pending: int = 0
    scope: str = ""
    closed_at: str | None = None


class VerdictBody(BaseModel):
    key: str = Field(min_length=1, max_length=400)
    verdict: str = Field(min_length=1, max_length=40)
    note: str | None = Field(default=None, max_length=2000)


# ── population + draw ────────────────────────────────────────────────────────
def _scope_clause(scope: SpotCheckScope) -> str:
    """WHERE tail defining the audited population.

    Note what is NOT filtered here: ``extraction_review_status``. An audit that
    skipped already-verified documents could never catch a bulk verify that
    was clicked through without reading — which is exactly the failure this
    queue exists to detect.
    """
    clause = "AND d.stitched_into IS NULL"
    if scope.score_min is not None:
        clause += " AND d.extraction_score >= $score_min"
    if scope.score_max is not None:
        clause += " AND d.extraction_score <= $score_max"
    if scope.batch is not None:
        clause += " AND d.extraction_batch = $batch"
    nt = _notice_type_clause(scope.notice_type, alias="d")
    if nt:
        clause += f" AND {nt}"
    dt = _date_exists_clause(scope.date_from, scope.date_to, alias="d")
    if dt:
        clause += f" AND {dt}"
    return clause


def _fetch_population(scope: SpotCheckScope) -> list[dict]:
    """Every document in scope, WITHOUT its extractions.

    Deliberately two queries. Applying a Cypher LIMIT here would make the draw
    "the alphabetically first N documents", which is a biased sample dressed up
    as a random one — so the whole in-scope population is listed and the seeded
    choice happens over all of it. That is only affordable because this query
    returns identifiers and timestamps, not the extraction JSON; the payload is
    fetched afterwards for the few hundred documents actually chosen
    (``_fetch_extractions``).
    """
    return run_read_query(
        f"""
        MATCH (d:Document)
        WHERE d.extraction_json IS NOT NULL {_scope_clause(scope)}
        RETURN d.filename AS filename,
               toString(d.extraction_at) AS extraction_at,
               toString(d.markdown_reextracted_at) AS markdown_reextracted_at,
               toString(d.markdown_loaded_at) AS markdown_loaded_at,
               toString(d.extraction_stale_at) AS extraction_stale_at
        ORDER BY d.filename
        """,
        {"score_min": scope.score_min, "score_max": scope.score_max,
         "batch": scope.batch,
         "date_from": scope.date_from, "date_to": scope.date_to},
        max_rows=100000,
        timeout=30.0,
    )


def _fetch_extractions(filenames: list[str]) -> dict[str, str]:
    """``extraction_json`` for the sampled documents only, keyed by filename."""
    if not filenames:
        return {}
    rows = run_read_query(
        """
        MATCH (d:Document)
        WHERE d.filename IN $fns AND d.extraction_json IS NOT NULL
        RETURN d.filename AS filename, d.extraction_json AS extraction_json
        """,
        {"fns": filenames}, max_rows=5000, timeout=30.0,
    )
    return {r["filename"]: r.get("extraction_json") or "[]" for r in rows}


def create_sample(body: SpotCheckCreateBody, by_email: str) -> dict:
    """Draw and persist one audit sample."""
    seed = body.seed if body.seed is not None else random.randrange(1, 2**31)
    rows = _fetch_population(body)

    # A stale extraction describes markdown that no longer exists, so its spans
    # cannot be judged against what the reviewer sees. Dropped from the
    # population and COUNTED, because "we audited the judgeable subset" is a
    # different claim from "we audited everything" and the difference belongs
    # in the report.
    fresh, excluded_stale = [], 0
    for r in rows:
        if extraction_stale(r.get("markdown_reextracted_at"),
                            r.get("markdown_loaded_at"),
                            r.get("extraction_at"),
                            r.get("extraction_stale_at")):
            excluded_stale += 1
        else:
            fresh.append(r)

    # Stage 1: seeded random subset of DOCUMENTS (sorted first so the draw does
    # not depend on the order Neo4j happened to return).
    rng = random.Random(seed)
    fresh.sort(key=lambda r: r["filename"])
    if len(fresh) > body.pool_documents:
        fresh = rng.sample(fresh, body.pool_documents)

    # Stage 2: stratified claim draw within those documents. Extractions are
    # fetched only now, for the chosen few, rather than for the whole corpus.
    payloads = _fetch_extractions([r["filename"] for r in fresh])
    claims = []
    for r in fresh:
        try:
            ents = json.loads(payloads.get(r["filename"]) or "[]")
        except json.JSONDecodeError:
            continue
        if isinstance(ents, list):
            claims.extend(iter_claims(r["filename"], ents))

    # Narrow BEFORE the draw, so the stratified spread runs over the requested
    # fields alone. Filtering afterwards would spend the sample on all ~60 field
    # types and then throw most of it away, leaving n=1 on what was asked for —
    # exactly the problem focusing is meant to solve.
    claims = filter_claims(claims, body.fields)

    picked = draw_sample(claims, size=body.size, seed=seed,
                         max_per_document=body.max_per_document)
    items = [{
        "key": c.key, "filename": c.filename, "field_id": c.field_id,
        "cls": c.cls, "attr": c.attr, "value": c.value,
        "span_text": c.span_text,
        "start": c.start, "end": c.end, "lot_index": c.lot_index,
        "stratum": c.stratum, "question": c.question,
    } for c in picked]

    sid = uuid.uuid4().hex[:12]
    scope = body.model_dump(include=set(SpotCheckScope.model_fields))
    run_query(
        """
        CREATE (s:SpotCheckSample {
            id: $id, created_at: datetime(), created_by: $by,
            seed: $seed, size: $size, scope_json: $scope,
            items_json: $items, verdicts_json: '{}',
            pool_documents: $pool, excluded_stale: $stale
        })
        """,
        {"id": sid, "by": by_email, "seed": seed, "size": len(items),
         "scope": json.dumps(scope), "items": json.dumps(items),
         "pool": len(fresh), "stale": excluded_stale},
    )
    return {
        "id": sid, "seed": seed, "size": len(items),
        "scope": describe_scope(scope),
        "documents_in_pool": len(fresh),
        "excluded_stale": excluded_stale,
        "claims_available": len(claims),
    }


def _load(sample_id: str) -> dict:
    rows = run_read_query(
        """
        MATCH (s:SpotCheckSample {id: $id})
        RETURN s.id AS id, toString(s.created_at) AS created_at,
               s.created_by AS created_by, s.seed AS seed,
               coalesce(s.scope_json, '{}') AS scope_json,
               coalesce(s.items_json, '[]') AS items_json,
               coalesce(s.verdicts_json, '{}') AS verdicts_json,
               toString(s.closed_at) AS closed_at,
               s.pool_documents AS pool_documents,
               s.excluded_stale AS excluded_stale
        LIMIT 1
        """,
        {"id": sample_id},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="sample not found")
    r = rows[0]
    try:
        items = json.loads(r["items_json"] or "[]")
    except json.JSONDecodeError:
        items = []
    try:
        verdicts = json.loads(r["verdicts_json"] or "{}")
    except json.JSONDecodeError:
        verdicts = {}
    try:
        scope = json.loads(r["scope_json"] or "{}")
    except json.JSONDecodeError:
        scope = {}
    return {**r, "items": items, "verdicts": verdicts, "scope": scope}


def _summary(row: dict) -> SpotCheckSummary:
    items, verdicts = row["items"], row["verdicts"]
    judged = sum(1 for it in items
                 if (verdicts.get(it.get("key")) or {}).get("verdict") in VERDICTS)
    return SpotCheckSummary(
        id=row["id"], created_at=row.get("created_at"),
        created_by=row.get("created_by"),
        seed=int(row["seed"]) if row.get("seed") is not None else None,
        size=len(items), judged=judged, pending=len(items) - judged,
        scope=describe_scope(row.get("scope") or {}),
        closed_at=row.get("closed_at"),
    )


# ── endpoints ────────────────────────────────────────────────────────────────
class SpotCheckCreateOut(BaseModel):
    id: str
    seed: int
    size: int
    scope: str
    documents_in_pool: int
    excluded_stale: int
    claims_available: int


@router.post("/samples", response_model=SpotCheckCreateOut)
def spotcheck_create(
    body: SpotCheckCreateBody,
    admin: UserOut = Depends(get_current_admin),
) -> SpotCheckCreateOut:
    out = create_sample(body, admin.email)
    if out["size"] == 0:
        raise HTTPException(
            status_code=400,
            detail=("no claims available in that scope — check the field names "
                    "(a typo matches nothing), or the scope is all stale / has "
                    "no extractions"))
    return SpotCheckCreateOut(**out)


@router.get("/samples", response_model=list[SpotCheckSummary])
def spotcheck_list(
    limit: int = Query(default=50, le=200),
    _admin: UserOut = Depends(get_current_admin),
) -> list[SpotCheckSummary]:
    rows = run_read_query(
        """
        MATCH (s:SpotCheckSample)
        RETURN s.id AS id, toString(s.created_at) AS created_at,
               s.created_by AS created_by, s.seed AS seed,
               coalesce(s.scope_json, '{}') AS scope_json,
               coalesce(s.items_json, '[]') AS items_json,
               coalesce(s.verdicts_json, '{}') AS verdicts_json,
               toString(s.closed_at) AS closed_at
        ORDER BY s.created_at DESC
        LIMIT $limit
        """,
        {"limit": limit}, max_rows=200,
    )
    out = []
    for r in rows:
        try:
            items = json.loads(r["items_json"] or "[]")
        except json.JSONDecodeError:
            items = []
        try:
            verdicts = json.loads(r["verdicts_json"] or "{}")
        except json.JSONDecodeError:
            verdicts = {}
        try:
            scope = json.loads(r["scope_json"] or "{}")
        except json.JSONDecodeError:
            scope = {}
        out.append(_summary({**r, "items": items, "verdicts": verdicts,
                             "scope": scope}))
    return out


@router.get("/samples/{sample_id}", response_model=SpotCheckSummary)
def spotcheck_get(
    sample_id: str,
    _admin: UserOut = Depends(get_current_admin),
) -> SpotCheckSummary:
    return _summary(_load(sample_id))


def _context_for(item: dict) -> dict:
    """Markdown window around the claim's span, re-anchored against current text."""
    rows = run_read_query(
        """
        MATCH (d:Document {filename: $fn})
        RETURN coalesce(d.stitched_markdown, d.markdown) AS markdown,
               d.public_url AS public_url, d.doc_type AS doc_type,
               toString(d.extraction_at) AS extraction_at,
               toString(d.markdown_reextracted_at) AS markdown_reextracted_at,
               toString(d.markdown_loaded_at) AS markdown_loaded_at,
               toString(d.extraction_stale_at) AS extraction_stale_at
        LIMIT 1
        """,
        {"fn": item.get("filename")},
    )
    if not rows:
        return {"context": "", "stale": True}
    r = rows[0]
    md = r.get("markdown") or ""
    stale = extraction_stale(r.get("markdown_reextracted_at"),
                             r.get("markdown_loaded_at"),
                             r.get("extraction_at"),
                             r.get("extraction_stale_at"))
    # Re-anchor the single entity so the highlight lands on the value as the
    # text reads NOW, not as it read when langextract ran. Anchoring is always
    # on `span_text` (the entity's own passage) — an attribute's value is often
    # a normalised label ("constructive") that never appears in the document.
    shim = [{"id": item.get("field_id"), "cls": item.get("cls"),
             "text": item.get("span_text") or item.get("value") or "",
             "start": item.get("start"), "end": item.get("end"),
             "attrs": {}}]
    anchored, _ = reanchor(shim, md or None, markdown_changed=stale)
    e = anchored[0] if anchored else {}
    start, end = e.get("start"), e.get("end")
    if start is None or end is None:
        # No span to centre on: show the head of the document rather than
        # nothing, and let `anchor` tell the reviewer why there is no highlight.
        return {"context": md[: _CONTEXT * 2], "context_start": None,
                "context_end": None, "public_url": r.get("public_url"),
                "doc_type": r.get("doc_type"), "stale": stale,
                "anchor": e.get("anchor", "none")}
    lo = max(0, start - _CONTEXT)
    hi = min(len(md), end + _CONTEXT)
    return {
        "context": md[lo:hi],
        "context_start": start - lo,
        "context_end": end - lo,
        "public_url": r.get("public_url"),
        "doc_type": r.get("doc_type"),
        "stale": stale,
        "anchor": e.get("anchor", "stored"),
    }


@router.get("/samples/{sample_id}/next", response_model=SpotCheckItemDetail)
def spotcheck_next(
    sample_id: str,
    at: str | None = Query(default=None, max_length=400),
    _admin: UserOut = Depends(get_current_admin),
) -> SpotCheckItemDetail:
    """The first unjudged item, or the item ``at`` that key when stepping back.

    ``at`` addresses a position in the frozen list rather than replaying a
    client-side history, so the back button cannot drift out of step with the
    sample the server holds.
    """
    row = _load(sample_id)
    items, verdicts = row["items"], row["verdicts"]
    if not items:
        raise HTTPException(status_code=404, detail="sample is empty")

    idx = None
    if at:
        for i, it in enumerate(items):
            if it.get("key") == at:
                idx = i
                break
    if idx is None:
        for i, it in enumerate(items):
            if (verdicts.get(it.get("key")) or {}).get("verdict") not in VERDICTS:
                idx = i
                break
    if idx is None:
        raise HTTPException(status_code=404, detail="sample complete")

    it = items[idx]
    rec = verdicts.get(it.get("key")) or {}
    # None values are dropped rather than passed through: the string fields
    # carry "" defaults, and a sample drawn before a field existed would
    # otherwise fail validation on None instead of falling back.
    return SpotCheckItemDetail(
        **{k: it[k] for k in
           ("key", "filename", "field_id", "cls", "attr", "value", "span_text",
            "question", "stratum", "start", "end", "lot_index")
           if it.get(k) is not None},
        verdict=rec.get("verdict"), note=rec.get("note"),
        index=idx + 1, total=len(items),
        **_context_for(it),
    )


@router.post("/samples/{sample_id}/verdict", response_model=SpotCheckSummary)
def spotcheck_verdict(
    sample_id: str,
    body: VerdictBody,
    admin: UserOut = Depends(get_current_admin),
) -> SpotCheckSummary:
    if body.verdict not in VERDICTS:
        raise HTTPException(status_code=400,
                            detail=f"verdict must be one of {list(VERDICTS)}")
    row = _load(sample_id)
    if not any(it.get("key") == body.key for it in row["items"]):
        # Refusing an unknown key keeps the frozen sample frozen — a verdict on
        # something that was never drawn would quietly widen the denominator.
        raise HTTPException(status_code=400, detail="key is not in this sample")
    verdicts = dict(row["verdicts"])
    verdicts[body.key] = {"verdict": body.verdict, "note": body.note,
                          "by": admin.email, "at": _now()}
    run_query(
        "MATCH (s:SpotCheckSample {id: $id}) SET s.verdicts_json = $v",
        {"id": sample_id, "v": json.dumps(verdicts)},
    )
    return _summary({**row, "verdicts": verdicts})


@router.get("/samples/{sample_id}/report")
def spotcheck_report(
    sample_id: str,
    _admin: UserOut = Depends(get_current_admin),
) -> dict:
    row = _load(sample_id)
    rep = build_report(
        {"id": row["id"], "seed": row.get("seed"), "scope": row.get("scope"),
         "items": row["items"]},
        row["verdicts"],
    )
    rep["excluded_stale"] = row.get("excluded_stale")
    rep["pool_documents"] = row.get("pool_documents")
    return rep
