"""
api/review/queries.py
---------------------
Cypher gateway for the enrichment review queue.
"""
from __future__ import annotations

from typing import Literal

from api.neo4j_client import run_query, run_read_query
from api.review.markdown_match import match_span, property_offset_in_notice


ReviewStatus = Literal["pending", "verified", "edited", "all"]

NoticeTypeFilter = Literal["all", "single", "multi", "unclassified"]


def _notice_type_clause(
    notice_type: NoticeTypeFilter | None,
    alias: str = "d",
) -> str | None:
    """Return a Cypher snippet to filter on `<alias>.notice_type`, or None
    when no filter applies. `alias` lets callers reuse the helper whether
    the Document is bound as `d` or via a path like `(a)-[:HAS_DOCUMENT]->(d)`."""
    if notice_type in (None, "all"):
        return None
    if notice_type == "single":
        return f"{alias}.notice_type = 'single'"
    if notice_type == "multi":
        return f"{alias}.notice_type = 'multi'"
    if notice_type == "unclassified":
        return f"{alias}.notice_type IS NULL"
    raise ValueError(f"unknown notice_type filter: {notice_type!r}")


def _date_exists_clause(
    date_from: str | None,
    date_to: str | None,
    alias: str = "d",
) -> str | None:
    """Cypher predicate: Document `alias` has any linked AuctionProperty whose
    auction_start_dt falls in the [date_from, date_to] window (inclusive).
    Returns None when neither bound is set.

    Documents in Neo4j can be linked to multiple AuctionProperty nodes
    (multi-notice). Using EXISTS keeps the document-centric row shape and
    surfaces the Document if *any* of its auctions match the date filter.
    Callers must also add `date_from` / `date_to` to their params dict.
    """
    if not date_from and not date_to:
        return None
    # `auction_start_dt` is stored as a Cypher DateTime (per
    # config/domain_ontology.yaml and scripts/load_tn_to_neo4j.py). Comparing
    # DateTime to Date directly returns NULL in Neo4j 5, so wrap the LHS in
    # date(...) to coerce — matches the working pattern at queries.py:129.
    return (
        "EXISTS { "
        f"MATCH ({alias})<-[:HAS_DOCUMENT]-(_a:AuctionProperty) "
        "WHERE _a.auction_start_dt IS NOT NULL "
        "AND ($date_from IS NULL OR date(_a.auction_start_dt) >= date($date_from)) "
        "AND ($date_to IS NULL OR date(_a.auction_start_dt) <= date($date_to)) "
        "}"
    )


def _sort_properties_by_markdown(row: dict) -> None:
    """Sort row['properties'] in place by their position in row['markdown'].

    Pops 'markdown' off the row afterwards so it doesn't bloat the response.

    A multi-property sales notice lists its lots in a specific order on the
    page. The reviewer wants the in-card property list to match that order
    so they can scroll the PDF top-to-bottom and tick lots in sequence.
    """
    md = row.pop("markdown", None) or ""
    props = row.get("properties") or []

    def sort_key(p: dict):
        off = property_offset_in_notice(p, md)
        if off is not None:
            return (0, off, p.get("auction_id") or "")
        # No price hit (or no markdown): fall back to reserve_price ASC, then aid.
        rp = p.get("reserve_price") or 0
        return (1, rp, p.get("auction_id") or "")

    props.sort(key=sort_key)






def list_notice_siblings(auction_id: str) -> dict | None:
    """Return the properties that share a sales notice with ``auction_id``.

    Powers the detail-view property switcher: a multi-property notice lists
    several lots, and the reviewer wants to step through them while keeping the
    notice image on the left — instead of bouncing back to the queue after each
    one.

    The "notice" is the property's primary linked Document — the one with a
    public_url if any (matching the detail view's source pane), else the first
    by filename. Siblings are ordered the same way the notice queue orders them
    (by position in the OCR markdown via ``_sort_properties_by_markdown``), so
    the switcher mirrors the order the lots appear on the page.

    Returns None when the property has no linked Document (nothing to switch
    between). When the property stands alone on its notice, ``properties`` holds
    just that one row, and the caller can choose to hide the switcher.
    """
    rows = run_read_query(
        """
        MATCH (a:AuctionProperty {auction_id: $aid})-[:HAS_DOCUMENT]->(d:Document)
        WITH d ORDER BY (CASE WHEN d.public_url IS NULL THEN 1 ELSE 0 END), d.filename
        WITH collect(d)[0] AS d
        WHERE d IS NOT NULL
        MATCH (d)<-[:HAS_DOCUMENT]-(sib:AuctionProperty)
        WHERE sib.description_source IN ['notice', 'human']
          AND sib.description IS NOT NULL
        OPTIONAL MATCH (sib)-[:HAS_BORROWER]->(b:Borrower)
        WITH d, sib, collect(DISTINCT b.name) AS borrowers
        WITH d, collect({
                auction_id:    sib.auction_id,
                title:         sib.title,
                borrowers:     borrowers,
                reserve_price: sib.reserve_price_num,
                completeness:  sib.description_completeness,
                source:        sib.description_source,
                verified:      coalesce(sib.description_verified, false),
                verified_at:   sib.description_verified_at,
                verified_by:   sib.description_verified_by
             }) AS properties
        RETURN d.filename     AS filename,
               d.file_path    AS file_path,
               d.public_url   AS public_url,
               d.notice_type  AS notice_type,
               d.markdown     AS markdown,
               properties     AS properties
        """,
        {"aid": auction_id},
        max_rows=1,
    )
    if not rows:
        return None
    row = rows[0]
    for p in row.get("properties") or []:
        v = p.get("verified_at")
        if v is not None and not isinstance(v, str):
            p["verified_at"] = str(v)
    _sort_properties_by_markdown(row)
    return row


def get_property(auction_id: str) -> dict | None:
    """Full review payload for one property: descriptions + linked Document."""
    rows = run_read_query(
        """
        MATCH (a:AuctionProperty {auction_id: $aid})
        OPTIONAL MATCH (a)-[:HAS_BORROWER]->(b:Borrower)
        OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(d:Document)
        OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(city:City)
        OPTIONAL MATCH (a)-[:LOCATED_IN_AREA]->(area:Area)
        WITH a, city, area,
             collect(DISTINCT b.name) AS borrowers,
             collect(DISTINCT d) AS docs
        RETURN a.auction_id                AS auction_id,
               a.title                     AS title,
               a.url                       AS url,
               a.reserve_price_num         AS reserve_price,
               toString(a.auction_start_dt) AS auction_start,
               city.name                   AS city,
               area.name                   AS area,
               borrowers                   AS borrowers,
               a.description               AS description,
               a.description_scraped       AS description_scraped,
               a.enriched_description      AS enriched_description,
               a.website_description       AS website_description,
               a.description_source        AS description_source,
               a.description_extracted     AS description_extracted_original,
               a.extracted_description     AS extracted_description,
               a.description_completeness  AS completeness,
               a.description_complete       AS complete,
               a.description_missing_parts  AS missing_parts,
               a.description_wrong_property AS wrong_property,
               a.description_judge_confidence AS judge_confidence,
               a.description_judge_reasoning  AS judge_reasoning,
               a.description_text_overlap   AS text_overlap,
               coalesce(a.description_verified, false) AS verified,
               a.description_verified_at   AS verified_at,
               a.description_verified_by   AS verified_by,
               a.description_review_notes  AS review_notes,
               [doc IN docs |
                  {filename: doc.filename,
                   file_path: doc.file_path,
                   public_url: doc.public_url,
                   storage_key: doc.storage_key,
                   notice_type: doc.notice_type,
                   markdown: doc.markdown}] AS documents
        """,
        {"aid": auction_id},
        max_rows=1,
    )
    if not rows:
        return None
    row = rows[0]
    _attach_property_highlight(row)
    return row


def _attach_property_highlight(row: dict) -> None:
    """Locate this property's block in its notice markdown and attach the raw
    char span as ``row['markdown_highlight'] = {'doc_index', 'start', 'end'}``
    (or None).

    The detail view shows one property, so this is a single span pointing at
    that auction's block inside a (possibly multi-property) notice. Probes with
    the website description, falling back to the notice-extracted description.

    A property can link to more than one Document (e.g. a per-lot crop plus the
    full notice); ``match_span`` is run against each and the best confident
    match wins, with ``doc_index`` telling the UI which document it belongs to.
    """
    row["markdown_highlight"] = None
    probe = row.get("website_description") or row.get("description") or ""
    if not probe:
        return
    best = None  # (score, doc_index, start, end)
    for i, doc in enumerate(row.get("documents") or []):
        md = (doc or {}).get("markdown")
        if not md:
            continue
        hit = match_span(probe, md, with_score=True)
        if hit and (best is None or hit[0] > best[0]):
            best = (hit[0], i, hit[1], hit[2])
    if best:
        row["markdown_highlight"] = {"doc_index": best[1], "start": best[2], "end": best[3]}


def verify(auction_id: str, by_email: str, notes: str | None) -> bool:
    """Mark verified. Returns True if a row was updated."""
    rows = run_query(
        """
        MATCH (a:AuctionProperty {auction_id: $aid})
        SET a.description_verified = true,
            a.description_verified_by = $by,
            a.description_verified_at = datetime(),
            a.description_review_notes = CASE WHEN $notes IS NULL OR $notes = ''
                                              THEN a.description_review_notes
                                              ELSE $notes END
        RETURN a.auction_id AS auction_id
        """,
        {"aid": auction_id, "by": by_email, "notes": notes},
    )
    return bool(rows)


def edit(auction_id: str, description: str, by_email: str, notes: str | None) -> bool:
    """Save an edited description, snapshotting the original on first edit
    so the LLM's output is recoverable. Sets source='human' and verified=true."""
    rows = run_query(
        """
        MATCH (a:AuctionProperty {auction_id: $aid})
        // First-edit snapshot — only set description_extracted if it's missing
        SET a.description_extracted = CASE
              WHEN a.description_extracted IS NULL OR a.description_extracted = ''
              THEN a.description
              ELSE a.description_extracted END,
            a.description = $desc,
            a.description_source = 'human',
            a.description_verified = true,
            a.description_verified_by = $by,
            a.description_verified_at = datetime(),
            a.description_review_notes = CASE WHEN $notes IS NULL OR $notes = ''
                                              THEN a.description_review_notes
                                              ELSE $notes END
        RETURN a.auction_id AS auction_id
        """,
        {"aid": auction_id, "desc": description, "by": by_email, "notes": notes},
    )
    return bool(rows)


def unverify(auction_id: str) -> bool:
    """Clear the verification flags. Description text is left intact."""
    rows = run_query(
        """
        MATCH (a:AuctionProperty {auction_id: $aid})
        REMOVE a.description_verified,
               a.description_verified_by,
               a.description_verified_at
        RETURN a.auction_id AS auction_id
        """,
        {"aid": auction_id},
    )
    return bool(rows)




ClassificationStatus = Literal["pending", "verified", "edited", "all"]


def _classification_where(
    status: ClassificationStatus,
    q: str | None = None,
    notice_type: NoticeTypeFilter | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[list[str], dict]:
    """Shared filter clause for the classification queue + bulk-confirm.

    Keeping list + bulk-confirm in the same WHERE prevents the two from
    drifting (which let bulk-confirm act on a different set than the count
    advertised on the button).
    """
    where = ["d.notice_type IS NOT NULL"]
    params: dict = {}
    if status == "pending":
        where.append("d.notice_type_verified_at IS NULL")
    elif status == "verified":
        where.append("d.notice_type_verified_at IS NOT NULL")
        where.append("coalesce(d.notice_type_overridden, false) = false")
    elif status == "edited":
        where.append("d.notice_type_verified_at IS NOT NULL")
        where.append("coalesce(d.notice_type_overridden, false) = true")
    # "all" → no extra filter

    if q:
        where.append("toLower(coalesce(d.filename, '')) CONTAINS toLower($q)")
        params["q"] = q

    nt_clause = _notice_type_clause(notice_type, alias="d")
    if nt_clause:
        where.append(nt_clause)

    date_clause = _date_exists_clause(date_from, date_to, alias="d")
    if date_clause:
        where.append(date_clause)
        params["date_from"] = date_from
        params["date_to"] = date_to

    return where, params


def list_classification_queue(
    status: ClassificationStatus = "pending",
    q: str | None = None,
    page: int = 1,
    size: int = 50,
    notice_type: NoticeTypeFilter | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Return a page of :Document nodes for the classification review queue.

    Status semantics:
      - pending:  not yet human-verified (notice_type_verified_at IS NULL)
      - verified: human confirmed, type unchanged (notice_type_overridden = false)
      - edited:   human overrode the type (notice_type_overridden = true)
      - all:      every Document with a notice_type

    date_from / date_to filter to Documents linked to any AuctionProperty
    whose auction_start_dt falls in the window.
    """
    page = max(1, int(page))
    size = max(1, min(200, int(size)))
    skip = (page - 1) * size

    where, params = _classification_where(
        status=status, q=q,
        notice_type=notice_type,
        date_from=date_from, date_to=date_to,
    )
    params["skip"] = skip
    params["size"] = size

    where_clause = " AND ".join(where)

    cypher = f"""
        MATCH (d:Document)
        WHERE {where_clause}
        OPTIONAL MATCH (d)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
        WITH d, collect(DISTINCT a.auction_id) AS auction_ids,
             collect(DISTINCT a.title) AS titles
        RETURN d.filename                       AS filename,
               d.file_path                      AS file_path,
               d.public_url                     AS public_url,
               d.notice_type                    AS notice_type,
               coalesce(d.property_count, size(auction_ids)) AS property_count,
               d.expected_lot_count             AS expected_lot_count,
               coalesce(d.notice_type_overridden, false) AS overridden,
               (d.notice_type_verified_at IS NOT NULL) AS verified,
               toString(d.notice_type_verified_at) AS verified_at,
               d.notice_type_verified_by        AS verified_by,
               d.notice_type_review_notes       AS review_notes,
               [t IN titles WHERE t IS NOT NULL][0..3] AS sample_titles,
               size(auction_ids)                AS auction_id_count
        ORDER BY verified ASC,
                 d.filename ASC
        SKIP $skip LIMIT $size
    """
    rows = run_read_query(cypher, params, max_rows=size, timeout=30.0)

    count_cypher = f"""
        MATCH (d:Document)
        WHERE {where_clause}
        RETURN count(d) AS total
    """
    count_rows = run_read_query(count_cypher, params, max_rows=1, timeout=30.0)
    total = count_rows[0]["total"] if count_rows else 0

    return {"page": page, "size": size, "total": total, "rows": rows}


def list_classification_queue_by_property(
    status: ClassificationStatus = "pending",
    q: str | None = None,
    page: int = 1,
    size: int = 50,
    notice_type: NoticeTypeFilter | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Return one row per AuctionProperty, projected with its Document's
    classification status. Filters mirror the by-document queue."""
    page = max(1, int(page))
    size = max(1, min(200, int(size)))
    skip = (page - 1) * size

    where = ["d.notice_type IS NOT NULL"]
    params: dict = {"skip": skip, "size": size}

    if status == "pending":
        where.append("d.notice_type_verified_at IS NULL")
    elif status == "verified":
        where.append("d.notice_type_verified_at IS NOT NULL")
        where.append("coalesce(d.notice_type_overridden, false) = false")
    elif status == "edited":
        where.append("d.notice_type_verified_at IS NOT NULL")
        where.append("coalesce(d.notice_type_overridden, false) = true")
    # "all" → no extra filter

    nt_clause = _notice_type_clause(notice_type, alias="d")
    if nt_clause:
        where.append(nt_clause)

    if q:
        where.append(
            "(toLower(coalesce(a.title, '')) CONTAINS toLower($q) "
            "OR toLower(coalesce(d.filename, '')) CONTAINS toLower($q))"
        )
        params["q"] = q

    if date_from:
        where.append("a.auction_start_dt >= date($date_from)")
        params["date_from"] = date_from
    if date_to:
        where.append("a.auction_start_dt <= date($date_to)")
        params["date_to"] = date_to

    where_clause = " AND ".join(where)
    cypher = f"""
        MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
        WHERE {where_clause}
        RETURN a.auction_id                       AS auction_id,
               a.title                            AS title,
               toString(a.auction_start_dt)       AS auction_start,
               a.reserve_price_num                AS reserve_price,
               d.filename                         AS notice_filename,
               d.notice_type                      AS notice_type,
               coalesce(d.notice_type_overridden, false) AS overridden,
               (d.notice_type_verified_at IS NOT NULL) AS verified,
               toString(d.notice_type_verified_at) AS verified_at
        ORDER BY verified ASC,
                 coalesce(a.auction_start_dt, date('9999-12-31')) ASC,
                 a.title ASC
        SKIP $skip LIMIT $size
    """
    rows = run_read_query(cypher, params, max_rows=size, timeout=30.0)

    count_cypher = f"""
        MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
        WHERE {where_clause}
        RETURN count(a) AS total
    """
    count_rows = run_read_query(count_cypher, params, max_rows=1, timeout=30.0)
    total = count_rows[0]["total"] if count_rows else 0
    return {"page": page, "size": size, "total": total, "rows": rows}


def classification_stats(
    notice_type: NoticeTypeFilter | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Counts for the classification queue header.

    date_from / date_to scope counts to Documents linked to an AuctionProperty
    whose auction_start_dt falls in the window — the pills must reflect the
    same filter that's narrowing the queue below.
    """
    extra: list[str] = []
    params: dict = {}
    nt_clause = _notice_type_clause(notice_type, alias="d")
    if nt_clause:
        extra.append(nt_clause)
    date_clause = _date_exists_clause(date_from, date_to, alias="d")
    if date_clause:
        extra.append(date_clause)
        params["date_from"] = date_from
        params["date_to"] = date_to
    extra_where = (" AND " + " AND ".join(extra)) if extra else ""
    rows = run_read_query(f"""
        MATCH (d:Document)
        WHERE d.notice_type IS NOT NULL{extra_where}
        RETURN
          count(*) AS total,
          sum(CASE WHEN d.notice_type_verified_at IS NULL THEN 1 ELSE 0 END) AS pending,
          sum(CASE WHEN d.notice_type_verified_at IS NOT NULL
                    AND coalesce(d.notice_type_overridden, false) = false
                   THEN 1 ELSE 0 END) AS verified,
          sum(CASE WHEN d.notice_type_verified_at IS NOT NULL
                    AND coalesce(d.notice_type_overridden, false) = true
                   THEN 1 ELSE 0 END) AS edited
    """, params, max_rows=1)
    if not rows:
        return {"total": 0, "pending": 0, "verified": 0, "edited": 0}
    r = rows[0]
    return {
        "total":    int(r.get("total") or 0),
        "pending":  int(r.get("pending") or 0),
        "verified": int(r.get("verified") or 0),
        "edited":   int(r.get("edited") or 0),
    }


def verify_classification(
    filename: str,
    notice_type: str,
    by_email: str,
    notes: str | None,
    expected_lot_count: int | None = None,
) -> dict | None:
    """Set a Document's notice_type from human review.

    ``expected_lot_count`` is the reviewer's count of lots in the notice —
    the downstream checksum for LangExtract (extracted lots must match).
    'single' implies 1, so it is stamped even when the reviewer omits it;
    for 'multi' the count is stored only when given.

    Side effects when the new notice_type differs from the prior:
      - Every linked AuctionProperty whose description was NOT human-edited
        is unverified (description_verified=false; audit fields cleared).
        The description text stays in place — it gets overwritten by the
        next LangExtract apply, after which the reviewer re-verifies.
      - Human-edited rows (description_source='human') are NEVER touched;
        their edits stand regardless of classification flips.

    Returns a result row or None if no Document had that filename.
    """
    if notice_type not in ("single", "multi"):
        raise ValueError("notice_type must be 'single' or 'multi'")
    if expected_lot_count is not None:
        if notice_type == "single" and expected_lot_count != 1:
            raise ValueError("a 'single' notice has exactly 1 lot")
        if notice_type == "multi" and expected_lot_count < 2:
            raise ValueError("a 'multi' notice has at least 2 lots")
    if expected_lot_count is None and notice_type == "single":
        expected_lot_count = 1
    params = {"filename": filename, "nt": notice_type,
              "by": by_email, "notes": notes,
              "elc": expected_lot_count}
    rows = run_query("""
        MATCH (d:Document {filename: $filename})
        WITH d, d.notice_type AS prior
        SET d.notice_type                  = $nt,
            d.expected_lot_count           = coalesce($elc, d.expected_lot_count),
            d.notice_type_overridden       = true,
            d.notice_type_verified_at      = datetime(),
            d.notice_type_verified_by      = $by,
            d.notice_type_review_notes     = CASE
                WHEN $notes IS NULL OR $notes = ''
                THEN d.notice_type_review_notes
                ELSE $notes END
        WITH d, prior
        OPTIONAL MATCH (d)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
        WHERE prior <> $nt
          AND coalesce(a.description_source, '') <> 'human'
        WITH d, collect(DISTINCT a) AS to_invalidate
        FOREACH (aa IN to_invalidate |
            REMOVE aa.description_verified,
                   aa.description_verified_by,
                   aa.description_verified_at
        )
        RETURN d.filename                          AS filename,
               d.notice_type                       AS notice_type,
               d.expected_lot_count                AS expected_lot_count,
               toString(d.notice_type_verified_at) AS verified_at,
               d.notice_type_verified_by           AS verified_by,
               d.notice_type_review_notes          AS review_notes,
               size(to_invalidate)                 AS invalidated_count
    """, params)
    return rows[0] if rows else None


def auto_confirm_classifications(
    by_email: str,
    notes: str | None = None,
    dry_run: bool = False,
    notice_type: NoticeTypeFilter | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
) -> dict:
    """Bulk-verify every pending Document that matches the reviewer's current
    queue filter (notice_type, date window, filename search).

    The reviewer's click on "Confirm all N in range" means "I've eyeballed
    the visible gallery and approve them all" — so we mirror the exact
    WHERE clause that produced the visible count. The shared
    `_classification_where` helper keeps the two queries in lockstep.

    The SET clause only stamps notice_type_verified_at / _by / _notes — it
    does NOT touch notice_type or notice_type_overridden, so no re-extract
    is triggered. (If the reviewer wants to FLIP a type, they use the per-
    notice verify endpoint, not bulk-confirm.)

    Returns ``{"count": N, "dry_run": bool}``. When ``dry_run=True`` nothing
    is written; the count reflects what *would* be confirmed.
    """
    where, params = _classification_where(
        status="pending",
        q=q,
        notice_type=notice_type,
        date_from=date_from,
        date_to=date_to,
    )
    params["by"] = by_email
    params["notes"] = notes
    where_clause = " AND ".join(where)

    if dry_run:
        rows = run_read_query(
            f"""
            MATCH (d:Document)
            WHERE {where_clause}
            RETURN count(d) AS n
            """,
            params,
            max_rows=1,
        )
        return {"count": int(rows[0]["n"]) if rows else 0, "dry_run": True}

    rows = run_query(
        f"""
        MATCH (d:Document)
        WHERE {where_clause}
        SET d.notice_type_verified_at  = datetime(),
            d.notice_type_verified_by  = $by,
            d.notice_type_review_notes = CASE
                WHEN $notes IS NULL OR $notes = ''
                THEN d.notice_type_review_notes
                ELSE $notes END,
            d.expected_lot_count = CASE
                WHEN d.notice_type = 'single'
                THEN coalesce(d.expected_lot_count, 1)
                ELSE d.expected_lot_count END
        RETURN count(d) AS n
        """,
        params,
    )
    return {"count": int(rows[0]["n"]) if rows else 0, "dry_run": False}




MarkdownStatus = Literal["pending", "verified", "edited", "all"]


def _parse_quality_where(pq_min: float | None,
                         pq_max: float | None) -> tuple[list[str], dict]:
    """WHERE fragments for the Datalab parse-quality filter (0–5, higher is
    better).

    Distinct from OCR health on purpose: health is our own text-only verdict
    (``pipeline/ocr_health.py``) and cannot see content the engine dropped,
    while parse quality is Datalab's own read on the *page*. A notice that
    lost a third of its text scores 100 health and ~3 parse quality, so the
    two filters answer different questions and compose.

    Documents with no stored score are excluded once either bound is set —
    same rule as the health filter, and the honest one here since a missing
    score means "never measured", not "fine".
    """
    where: list[str] = []
    params: dict = {}
    if pq_min is not None:
        where.append("d.parse_quality_score IS NOT NULL")
        where.append("d.parse_quality_score >= $pq_min")
        params["pq_min"] = float(pq_min)
    if pq_max is not None:
        where.append("d.parse_quality_score IS NOT NULL")
        where.append("d.parse_quality_score <= $pq_max")
        params["pq_max"] = float(pq_max)
    return where, params


def _health_flags_where(flags: list[str] | None) -> tuple[list[str], dict]:
    """WHERE fragment matching Documents carrying ANY of ``flags``.

    OR, not AND: the reviewer picking `missing-region` + `repetition` wants the
    queue of everything broken in either way, not the rare doc broken in both.
    An empty/None list is no filter at all.
    """
    if not flags:
        return [], {}
    return (["any(f IN coalesce(d.ocr_health_flags, []) WHERE f IN $health_flags)"],
            {"health_flags": list(flags)})


def _markdown_where(
    status: MarkdownStatus,
    score_min: float | None,
    score_max: float | None = None,
    notice_type: NoticeTypeFilter | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    pq_min: float | None = None,
    pq_max: float | None = None,
    flags: list[str] | None = None,
) -> tuple[list[str], dict]:
    where = ["d.markdown IS NOT NULL", "d.markdown <> ''"]
    params: dict = {}
    if status == "pending":
        where.append("d.markdown_verified_at IS NULL")
    elif status == "verified":
        where.append("d.markdown_verified_at IS NOT NULL")
        where.append("d.markdown_quality = 'good'")
    elif status == "edited":
        where.append("d.markdown_verified_at IS NOT NULL")
        where.append("d.markdown_quality = 'bad'")
    # "all" → no extra filter
    # The markdown stage's single score is OCR-health (pipeline/ocr_health.py) —
    # the intrinsic image→markdown verdict — so the score filter targets it, not
    # the coverage-vs-website markdown_quality_score.
    if score_min is not None:
        where.append("d.ocr_health_score IS NOT NULL")
        where.append("d.ocr_health_score >= $score_min")
        params["score_min"] = float(score_min)
    if score_max is not None:
        where.append("d.ocr_health_score IS NOT NULL")
        where.append("d.ocr_health_score <= $score_max")
        params["score_max"] = float(score_max)
    pq_where, pq_params = _parse_quality_where(pq_min, pq_max)
    where.extend(pq_where)
    params.update(pq_params)
    fl_where, fl_params = _health_flags_where(flags)
    where.extend(fl_where)
    params.update(fl_params)
    clause = _notice_type_clause(notice_type, alias="d")
    if clause:
        where.append(clause)
    if q:
        where.append("toLower(coalesce(d.filename, '')) CONTAINS toLower($q)")
        params["q"] = q
    date_clause = _date_exists_clause(date_from, date_to, alias="d")
    if date_clause:
        where.append(date_clause)
        params["date_from"] = date_from
        params["date_to"] = date_to
    return where, params


def markdown_stats(
    score_min: float = 70.0,
    score_max: float = 100.0,
    notice_type: NoticeTypeFilter | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Counts for the markdown review header.

    `auto_confirmable` = unverified Documents whose score ≥ score_min and
    ≤ score_max — the pile the bulk-confirm button would clear at the current
    slider values.

    date_from / date_to filter to Documents linked to any AuctionProperty
    whose auction_start_dt falls in the window, so the header pills track
    the same date filter the reviewer applied.
    """
    extra: list[str] = []
    params: dict = {"score_min": float(score_min), "score_max": float(score_max)}
    nt_clause = _notice_type_clause(notice_type, alias="d")
    if nt_clause:
        extra.append(nt_clause)
    date_clause = _date_exists_clause(date_from, date_to, alias="d")
    if date_clause:
        extra.append(date_clause)
        params["date_from"] = date_from
        params["date_to"] = date_to
    extra_where = (" AND " + " AND ".join(extra)) if extra else ""
    rows = run_read_query(
        f"""
        MATCH (d:Document)
        WHERE d.markdown IS NOT NULL AND d.markdown <> ''{extra_where}
        RETURN
          count(*) AS total,
          sum(CASE WHEN d.markdown_verified_at IS NULL THEN 1 ELSE 0 END) AS pending,
          sum(CASE WHEN d.markdown_verified_at IS NOT NULL
                    AND d.markdown_quality = 'good' THEN 1 ELSE 0 END) AS verified,
          sum(CASE WHEN d.markdown_verified_at IS NOT NULL
                    AND d.markdown_quality = 'bad' THEN 1 ELSE 0 END) AS edited,
          sum(CASE WHEN d.markdown_verified_at IS NULL
                    AND d.markdown_quality_score IS NOT NULL
                    AND d.markdown_quality_score >= $score_min
                    AND d.markdown_quality_score <= $score_max
                   THEN 1 ELSE 0 END) AS auto_confirmable
        """,
        params,
        max_rows=1,
    )
    if not rows:
        return {"total": 0, "pending": 0, "verified": 0, "edited": 0, "auto_confirmable": 0}
    r = rows[0]
    return {
        "total":            int(r.get("total") or 0),
        "pending":          int(r.get("pending") or 0),
        "verified":         int(r.get("verified") or 0),
        "edited":           int(r.get("edited") or 0),
        "auto_confirmable": int(r.get("auto_confirmable") or 0),
    }


def _attach_markdown_highlights(rows: list[dict]) -> None:
    """For each Document row, turn its linked properties' website descriptions
    into highlight spans over the OCR markdown, then drop the raw descriptions.

    Adds ``row['highlights'] = [{'start': int, 'end': int}, ...]`` (raw markdown
    character offsets), so the review UI can mark where each property in the DB
    sits inside the notice — handy on multi-property notices where only some
    lots are tracked.
    """
    for row in rows:
        descriptions = row.pop("website_descriptions", None) or []
        markdown = row.get("markdown") or ""
        spans: list[tuple[int, int]] = []
        if markdown:
            for desc in descriptions:
                span = match_span(desc, markdown)
                if span and span not in spans:
                    spans.append(span)
        spans.sort()
        row["highlights"] = [{"start": s, "end": e} for s, e in spans]


def list_markdown_queue(
    status: MarkdownStatus = "pending",
    q: str | None = None,
    page: int = 1,
    size: int = 50,
    score_min: float | None = None,
    score_max: float | None = None,
    notice_type: NoticeTypeFilter | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    pq_min: float | None = None,
    pq_max: float | None = None,
    flags: list[str] | None = None,
) -> dict:
    """Return a page of Documents for markdown-quality review.

    Order: pending first, then lowest OCR-health first (so the worst OCR
    floats to the top of the reviewer's queue). `score_min`/`score_max`
    filter on ocr_health_score — the markdown stage's single score.
    `pq_min`/`pq_max` filter on Datalab's parse_quality_score (0–5).

    date_from / date_to filter to Documents linked to any AuctionProperty
    whose auction_start_dt falls in the window.
    """
    page = max(1, int(page))
    size = max(1, min(200, int(size)))
    skip = (page - 1) * size

    where, params = _markdown_where(
        status, score_min, score_max, notice_type,
        date_from=date_from, date_to=date_to, q=q,
        pq_min=pq_min, pq_max=pq_max, flags=flags,
    )
    params.update({"skip": skip, "size": size})
    where_clause = " AND ".join(where)

    cypher = f"""
        MATCH (d:Document)
        WHERE {where_clause}
        OPTIONAL MATCH (d)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
        WITH d, count(DISTINCT a) AS prop_count,
             collect(DISTINCT a.website_description) AS website_descriptions
        RETURN d.filename                       AS filename,
               d.file_path                      AS file_path,
               d.public_url                     AS public_url,
               d.notice_type                    AS notice_type,
               prop_count                       AS property_count,
               website_descriptions             AS website_descriptions,
               size(d.markdown)                 AS markdown_length,
               d.markdown                       AS markdown,
               d.markdown_model                 AS markdown_model,
               d.markdown_quality_score         AS score,
               d.ocr_health_score               AS ocr_health_score,
               d.ocr_health_flags               AS ocr_health_flags,
               d.parse_quality_score            AS parse_quality_score,
               d.ink_uncovered_ratio            AS ink_uncovered_ratio,
               d.markdown_quality               AS quality,
               (d.markdown_verified_at IS NOT NULL) AS verified,
               toString(d.markdown_verified_at) AS verified_at,
               d.markdown_verified_by           AS verified_by,
               d.markdown_review_notes          AS review_notes,
               toString(d.markdown_reextracted_at) AS reextracted_at,
               d.markdown_reextracted_by        AS reextracted_by
        ORDER BY (d.markdown_verified_at IS NULL) DESC,
                 coalesce(d.ocr_health_score, -1) ASC,
                 d.filename ASC
        SKIP $skip LIMIT $size
    """
    rows = run_read_query(cypher, params, max_rows=size, timeout=30.0)

    count_cypher = f"""
        MATCH (d:Document)
        WHERE {where_clause}
        RETURN count(d) AS total
    """
    count_rows = run_read_query(count_cypher, params, max_rows=1, timeout=30.0)
    total = count_rows[0]["total"] if count_rows else 0

    return {"page": page, "size": size, "total": total, "rows": rows}


def list_markdown_queue_by_property(
    status: MarkdownStatus = "pending",
    q: str | None = None,
    page: int = 1,
    size: int = 50,
    score_min: float | None = None,
    score_max: float | None = None,
    notice_type: NoticeTypeFilter | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    pq_min: float | None = None,
    pq_max: float | None = None,
    flags: list[str] | None = None,
) -> dict:
    """One row per AuctionProperty projected with its Document's
    markdown-quality status."""
    page = max(1, int(page))
    size = max(1, min(200, int(size)))
    skip = (page - 1) * size

    where = ["d.markdown IS NOT NULL", "d.markdown <> ''"]
    params: dict = {"skip": skip, "size": size}

    if status == "pending":
        where.append("d.markdown_verified_at IS NULL")
    elif status == "verified":
        where.append("d.markdown_verified_at IS NOT NULL")
        where.append("d.markdown_quality = 'good'")
    elif status == "edited":
        where.append("d.markdown_verified_at IS NOT NULL")
        where.append("d.markdown_quality = 'bad'")
    # "all" → no extra filter

    # OCR-health is the markdown stage's single score (see list_markdown_queue).
    if score_min is not None:
        where.append("d.ocr_health_score IS NOT NULL")
        where.append("d.ocr_health_score >= $score_min")
        params["score_min"] = float(score_min)
    if score_max is not None:
        where.append("d.ocr_health_score IS NOT NULL")
        where.append("d.ocr_health_score <= $score_max")
        params["score_max"] = float(score_max)

    pq_where, pq_params = _parse_quality_where(pq_min, pq_max)
    where.extend(pq_where)
    params.update(pq_params)

    fl_where, fl_params = _health_flags_where(flags)
    where.extend(fl_where)
    params.update(fl_params)

    nt_clause = _notice_type_clause(notice_type, alias="d")
    if nt_clause:
        where.append(nt_clause)

    if q:
        where.append(
            "(toLower(coalesce(a.title, '')) CONTAINS toLower($q) "
            "OR toLower(coalesce(d.filename, '')) CONTAINS toLower($q))"
        )
        params["q"] = q

    if date_from:
        where.append("a.auction_start_dt >= date($date_from)")
        params["date_from"] = date_from
    if date_to:
        where.append("a.auction_start_dt <= date($date_to)")
        params["date_to"] = date_to

    where_clause = " AND ".join(where)
    cypher = f"""
        MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
        WHERE {where_clause}
        RETURN a.auction_id                       AS auction_id,
               a.title                            AS title,
               toString(a.auction_start_dt)       AS auction_start,
               a.reserve_price_num                AS reserve_price,
               d.filename                         AS notice_filename,
               d.notice_type                      AS notice_type,
               d.markdown_quality_score           AS score,
               d.ocr_health_score                 AS ocr_health_score,
               d.ocr_health_flags                 AS ocr_health_flags,
               d.parse_quality_score              AS parse_quality_score,
               d.markdown_quality                 AS quality,
               (d.markdown_verified_at IS NOT NULL) AS verified,
               toString(d.markdown_verified_at)   AS verified_at
        ORDER BY verified ASC,
                 coalesce(d.ocr_health_score, -1) ASC,
                 a.title ASC
        SKIP $skip LIMIT $size
    """
    rows = run_read_query(cypher, params, max_rows=size, timeout=30.0)
    count_cypher = f"""
        MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
        WHERE {where_clause}
        RETURN count(a) AS total
    """
    count_rows = run_read_query(count_cypher, params, max_rows=1, timeout=30.0)
    total = count_rows[0]["total"] if count_rows else 0
    return {"page": page, "size": size, "total": total, "rows": rows}


def verify_markdown(
    filename: str,
    quality: str,
    by_email: str,
    notes: str | None,
) -> dict | None:
    """Mark a Document's markdown as 'good' or 'bad'. Returns the updated row."""
    if quality not in ("good", "bad"):
        raise ValueError("quality must be 'good' or 'bad'")
    rows = run_query(
        """
        MATCH (d:Document {filename: $filename})
        SET d.markdown_quality           = $quality,
            d.markdown_verified_at       = datetime(),
            d.markdown_verified_by       = $by,
            d.markdown_review_notes      = CASE
                WHEN $notes IS NULL OR $notes = ''
                THEN d.markdown_review_notes
                ELSE $notes END
        RETURN d.filename                       AS filename,
               d.file_path                      AS file_path,
               d.public_url                     AS public_url,
               d.notice_type                    AS notice_type,
               d.property_count                 AS property_count,
               size(d.markdown)                 AS markdown_length,
               d.markdown                       AS markdown,
               d.markdown_model                 AS markdown_model,
               d.markdown_quality_score         AS score,
               d.ocr_health_score               AS ocr_health_score,
               d.ocr_health_flags               AS ocr_health_flags,
               d.parse_quality_score            AS parse_quality_score,
               d.markdown_quality               AS quality,
               true                             AS verified,
               toString(d.markdown_verified_at) AS verified_at,
               d.markdown_verified_by           AS verified_by,
               d.markdown_review_notes          AS review_notes,
               toString(d.markdown_reextracted_at) AS reextracted_at,
               d.markdown_reextracted_by        AS reextracted_by
        """,
        {"filename": filename, "quality": quality, "by": by_email, "notes": notes},
    )
    return rows[0] if rows else None


def auto_confirm_markdown(
    score_min: float,
    by_email: str,
    notes: str | None = None,
    dry_run: bool = False,
    score_max: float = 100.0,
    notice_type: NoticeTypeFilter | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    pq_min: float | None = None,
    pq_max: float | None = None,
    flags: list[str] | None = None,
) -> dict:
    """Bulk-verify (quality='good') every pending Document that matches the
    reviewer's current queue filter (score range, parse-quality range, health
    flags, notice_type, date window, filename search). Mirrors `list_markdown_queue`
    via the shared `_markdown_where` helper so the count and the action stay
    aligned — the parse-quality bounds MUST be threaded through here too, or
    the button confirms a wider set than the queue it is labelled with.

    Returns ``{"count": N, "dry_run": bool}``.
    """
    where, params = _markdown_where(
        status="pending",
        score_min=score_min,
        score_max=score_max,
        notice_type=notice_type,
        date_from=date_from,
        date_to=date_to,
        q=q,
        pq_min=pq_min,
        pq_max=pq_max,
        flags=flags,
    )
    params["by"] = by_email
    params["notes"] = notes
    where_clause = " AND ".join(where)

    if dry_run:
        rows = run_read_query(
            f"""
            MATCH (d:Document)
            WHERE {where_clause}
            RETURN count(d) AS n
            """,
            params,
            max_rows=1,
        )
        return {"count": int(rows[0]["n"]) if rows else 0, "dry_run": True}

    rows = run_query(
        f"""
        MATCH (d:Document)
        WHERE {where_clause}
        SET d.markdown_quality      = 'good',
            d.markdown_verified_at  = datetime(),
            d.markdown_verified_by  = $by,
            d.markdown_review_notes = CASE
                WHEN $notes IS NULL OR $notes = ''
                THEN d.markdown_review_notes
                ELSE $notes END
        RETURN count(d) AS n
        """,
        params,
    )
    return {"count": int(rows[0]["n"]) if rows else 0, "dry_run": False}


# ── Pipeline overview ───────────────────────────────────────────────────────

# The workflow a notice moves through, in the order it actually runs: each
# machine step is followed by the human gate that accepts it. Classification
# comes before OCR because notice_type routes the OCR tier (single -> fast,
# multi -> accurate, see pipeline.config.datalab_mode_for), so a notice whose
# type nobody confirmed has not really cleared the step that feeds the parser.
#
# Counts are CUMULATIVE — a document counts at stage N only if it cleared
# 1..N-1. Counted independently the corpus reports more notices extracted
# (1,553) than markdown-verified (1,489), because extraction has run ahead of
# review; every "drop" between stages would then be fiction.
#
# Block layer and ink coverage are deliberately absent: they are measurements
# taken alongside the workflow, not steps in it, and they surface under
# "attention" instead.
PIPELINE_STAGES: list[tuple[str, str, str]] = [
    ("scraped",       "Scraped",              "true"),
    ("classified",    "Classified single/multi",
     "d.notice_type IS NOT NULL"),
    ("class_ok",      "Classification reviewed",
     "d.notice_type_verified_at IS NOT NULL"),
    ("ocr",           "OCR'd",
     "d.markdown IS NOT NULL AND d.markdown <> ''"),
    ("ocr_ok",        "OCR reviewed",
     "d.markdown_verified_at IS NOT NULL"),
    ("extracted",     "Entities extracted",
     "d.extraction_json IS NOT NULL"),
    ("extract_ok",    "Extraction reviewed",
     "coalesce(d.extraction_review_status,'pending') = 'verified'"),
]

# Stages the pipeline will grow but does not have yet. Carried here so the
# dashboard shows the whole intended path — a funnel that stops at extraction
# implies the work ends there. They report no count rather than a zero, because
# "nothing has reached this stage" and "this stage does not exist" are different
# facts and a 0 would read as the first.
PIPELINE_PLANNED: list[tuple[str, str]] = [
    ("entity_resolution", "Entity resolution"),
    ("graph_loaded",      "Loaded into the graph"),
]


def _stage_counts(scope_match: str, scope_where: str, params: dict) -> list[dict]:
    """Count Documents clearing each stage AND all stages before it."""
    parts = []
    for i, (key, _label, _pred) in enumerate(PIPELINE_STAGES):
        chain = " AND ".join(f"({p})" for _k, _l, p in PIPELINE_STAGES[: i + 1])
        parts.append(f"sum(CASE WHEN {chain} THEN 1 ELSE 0 END) AS {key}")
    rows = run_read_query(
        f"{scope_match} {scope_where} WITH DISTINCT d RETURN {', '.join(parts)}",
        params, max_rows=1, timeout=30.0)
    r = rows[0] if rows else {}
    out = [{"key": key, "label": label, "count": int(r.get(key) or 0),
            "planned": False}
           for key, label, _pred in PIPELINE_STAGES]
    out += [{"key": key, "label": label, "count": None, "planned": True}
            for key, label in PIPELINE_PLANNED]
    return out


def pipeline_overview() -> dict:
    """Corpus-wide funnel plus the same funnel for upcoming auctions only.

    Two scopes because they answer different questions: the whole corpus says
    where the pipeline leaks, while the upcoming slice says what is at risk for
    an auction that has not happened yet — the only backlog with a deadline.
    """
    all_stages = _stage_counts("MATCH (d:Document)", "", {})
    upcoming = _stage_counts(
        "MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)",
        "WHERE a.auction_start_dt >= datetime()", {})

    flag_rows = run_read_query(
        """
        MATCH (d:Document)
        WHERE size(coalesce(d.ocr_health_flags, [])) > 0
        UNWIND d.ocr_health_flags AS f
        WITH f, count(*) AS n
        OPTIONAL MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d2:Document)
        WHERE f IN coalesce(d2.ocr_health_flags, [])
          AND a.auction_start_dt >= datetime()
        RETURN f AS flag, n AS total, count(DISTINCT d2) AS upcoming
        ORDER BY n DESC
        """,
        max_rows=20, timeout=30.0)

    extra_rows = run_read_query(
        """
        MATCH (d:Document)
        WITH d, (d.markdown IS NOT NULL AND d.markdown <> '') AS has_md
        RETURN sum(CASE WHEN coalesce(d.extraction_review_status,'') = 'pending'
                         AND d.extraction_json IS NOT NULL THEN 1 ELSE 0 END)
                   AS extraction_pending,
               sum(CASE WHEN d.extraction_stale_at IS NOT NULL THEN 1 ELSE 0 END)
                   AS extraction_stale,
               sum(CASE WHEN has_md AND d.ink_uncovered_ratio IS NULL
                        THEN 1 ELSE 0 END) AS unmeasured,
               sum(CASE WHEN has_md AND (d.blocks IS NULL OR d.blocks = '')
                        THEN 1 ELSE 0 END) AS no_blocks,
               sum(CASE WHEN d.parse_quality_score IS NOT NULL THEN 1 ELSE 0 END)
                   AS parse_quality_scored
        """,
        max_rows=1, timeout=30.0)
    extra = extra_rows[0] if extra_rows else {}

    return {
        "stages": all_stages,
        "upcoming_stages": upcoming,
        "flags": [{"flag": r["flag"], "total": int(r["total"] or 0),
                   "upcoming": int(r["upcoming"] or 0)} for r in flag_rows],
        "extraction_pending": int(extra.get("extraction_pending") or 0),
        "extraction_stale": int(extra.get("extraction_stale") or 0),
        "unmeasured": int(extra.get("unmeasured") or 0),
        "no_blocks": int(extra.get("no_blocks") or 0),
        "parse_quality_scored": int(extra.get("parse_quality_scored") or 0),
    }
