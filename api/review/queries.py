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
    # Resolved means both resolvers have been over the notice: its lender
    # (document-level) and its properties' places. Documents with no linked
    # property have no places to resolve, so they clear on the lender alone.
    ("resolved",      "Entities resolved",
     "d.entity_resolved_at IS NOT NULL AND (d.place_resolved_at IS NOT NULL "
     "OR NOT EXISTS { MATCH (:AuctionProperty)-[:HAS_DOCUMENT]->(d) })"),
    # Resolution review is corpus-shaped, not per-document — a human rules on
    # lookalike pairs and conflict patterns, and each verdict settles every
    # notice it touches. So "reviewed" is the absence of open questions: the
    # attention flags are recomputed by the resolvers on every run, and this
    # stage advances as decisions land and the scripts re-apply them.
    ("resolve_ok",    "Resolution reviewed",
     "d.bank_attention IS NULL AND d.place_attention IS NULL "
     "AND d.branch_attention IS NULL"),
]

# Stages the pipeline will grow but does not have yet. Carried here so the
# dashboard shows the whole intended path — a funnel that stops at extraction
# implies the work ends there. They report no count rather than a zero, because
# "nothing has reached this stage" and "this stage does not exist" are different
# facts and a 0 would read as the first.
PIPELINE_PLANNED: list[tuple[str, str]] = [
    ("graph_loaded", "Loaded into the graph"),
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


# ── Per-stage detail ────────────────────────────────────────────────────────
#
# Each stage of the funnel opens its own page. The endpoint returns *panels* in
# a small generic shape — a title, a note, and rows of {label, count, pct,
# href} — so the renderer stays one function and a new stage only has to
# describe what it knows. Rows carry an optional href into the queue already
# filtered, keeping the dashboard a way in rather than a dead end.

# Entity-coverage is computed from stored extraction_json, which means pulling
# ~13 KB per document; the full corpus is ~20 MB and too slow to fetch on every
# page load. The panel therefore scans the most recent N extractions and says
# so — for "is the extractor capturing the fields we need", recent behaviour is
# the honest sample anyway.
ENTITY_COVERAGE_SAMPLE = 600

_stage_cache: dict = {}
_STAGE_CACHE_TTL_S = 300.0


def _rows(pairs, total, href=None) -> list[dict]:
    """[(label, count)] -> panel rows carrying their share of ``total``."""
    return [{"label": label, "count": int(count or 0),
             "pct": round((count or 0) / total * 100, 1) if total else 0.0,
             "href": href(label) if href else None}
            for label, count in pairs]


def _count_query(cypher: str, params: dict | None = None) -> dict:
    rows = run_read_query(cypher, params or {}, max_rows=1, timeout=30.0)
    return rows[0] if rows else {}


def _panel(title: str, rows: list[dict], note: str = "", kind: str = "bars") -> dict:
    return {"kind": kind, "title": title, "note": note, "rows": rows}


def _entity_coverage_panels(sample: int) -> list[dict]:
    """Field coverage + score bands + validator issues over recent extractions."""
    import collections
    import json as _json
    import time as _time

    cached = _stage_cache.get("entity_coverage")
    if cached and (_time.monotonic() - cached[0]) < _STAGE_CACHE_TTL_S:
        return cached[1]

    from pipeline.validators import COVERAGE_FIELDS, validate_stored

    rows = run_read_query(
        """
        MATCH (d:Document)
        WHERE d.extraction_json IS NOT NULL
        RETURN d.filename AS filename, d.extraction_json AS ej,
               d.extraction_score AS score
        ORDER BY d.extraction_at DESC
        LIMIT $lim
        """,
        {"lim": int(sample)}, max_rows=int(sample), timeout=120.0)

    cov = collections.Counter()
    issues = collections.Counter()
    ent_counts: list[int] = []
    n = len(rows)
    for r in rows:
        try:
            ents = _json.loads(r.get("ej") or "[]")
        except (TypeError, ValueError):
            continue
        ent_counts.append(len(ents))
        report = validate_stored(ents)
        for f in report.get("fields") or []:
            cov[f] += 1
        for iss in report.get("issues") or []:
            issues[iss.get("code") or "?"] += 1

    field_rows = sorted(
        _rows([(f, cov[f]) for f in COVERAGE_FIELDS], n),
        key=lambda r: r["pct"])
    issue_rows = _rows(issues.most_common(12), n)
    ent_counts.sort()
    median_ents = ent_counts[len(ent_counts) // 2] if ent_counts else 0

    panels = [
        _panel("Entity coverage", field_rows,
               f"share of the last {n} extractions carrying each field — "
               "lowest first, because that is where the extractor is losing "
               "information"),
        _panel("Validator issues", issue_rows,
               "how often each check fires across those extractions"),
        _panel("Entities per notice", _rows([
            ("fewest", ent_counts[0] if ent_counts else 0),
            ("median", median_ents),
            ("most", ent_counts[-1] if ent_counts else 0),
        ], 0), "raw counts, not percentages", kind="lines"),
    ]
    _stage_cache["entity_coverage"] = (_time.monotonic(), panels)
    return panels


def pipeline_stage_detail(key: str, sample: int = ENTITY_COVERAGE_SAMPLE) -> dict:
    """Panels describing one pipeline stage in depth."""
    labels = {k: label for k, label, _p in PIPELINE_STAGES}
    labels.update({k: label for k, label in PIPELINE_PLANNED})
    if key not in labels:
        raise ValueError(f"unknown stage: {key}")
    out = {"key": key, "label": labels[key], "panels": []}

    if key in {k for k, _l in PIPELINE_PLANNED}:
        out["panels"] = [_panel(
            "Not built yet",
            [], f"{labels[key]} is planned; nothing reports on it yet.")]
        return out

    if key == "scraped":
        c = _count_query("""
            MATCH (d:Document)
            RETURN count(d) AS total,
                   sum(CASE WHEN d.public_url IS NULL OR d.public_url = ''
                            THEN 1 ELSE 0 END) AS no_url,
                   sum(CASE WHEN d.markdown IS NULL OR d.markdown = ''
                            THEN 1 ELSE 0 END) AS no_text,
                   sum(CASE WHEN NOT EXISTS {
                            MATCH (:AuctionProperty)-[:HAS_DOCUMENT]->(d) }
                            THEN 1 ELSE 0 END) AS orphan
        """)
        total = int(c.get("total") or 0)
        out["panels"] = [
            _panel("Source material", _rows([
                ("notices scraped", total),
                ("no source URL — cannot be re-read", c.get("no_url")),
                ("no text yet", c.get("no_text")),
                ("not linked to any auction", c.get("orphan")),
            ], total), "everything downstream starts from these"),
            _panel("By file type", _rows([
                (r["t"], r["n"]) for r in run_read_query(
                    "MATCH (d:Document) RETURN coalesce(d.file_type,'unknown') AS t, "
                    "count(*) AS n ORDER BY n DESC", max_rows=20, timeout=30.0)
            ], total), "PDFs cannot be checked for missing content"),
        ]

    elif key in ("classified", "class_ok"):
        total = int(_count_query("MATCH (d:Document) RETURN count(d) AS n").get("n") or 0)
        type_rows = run_read_query(
            "MATCH (d:Document) RETURN coalesce(d.notice_type,'unclassified') AS t, "
            "count(*) AS n ORDER BY n DESC", max_rows=10, timeout=30.0)
        c = _count_query("""
            MATCH (d:Document)
            RETURN sum(CASE WHEN d.notice_type_verified_at IS NOT NULL
                            THEN 1 ELSE 0 END) AS verified,
                   sum(CASE WHEN coalesce(d.notice_type_overridden,false)
                            THEN 1 ELSE 0 END) AS overridden,
                   sum(CASE WHEN d.notice_type = 'multi'
                             AND d.expected_lot_count IS NULL
                            THEN 1 ELSE 0 END) AS multi_no_lots
        """)
        out["panels"] = [
            _panel("Type mix", _rows([(r["t"], r["n"]) for r in type_rows], total),
                   "single routes OCR to the fast tier, multi to accurate",
                   ),
            _panel("Review", _rows([
                ("confirmed by a human", c.get("verified")),
                ("still to confirm", total - int(c.get("verified") or 0)),
                ("human overrode the machine", c.get("overridden")),
                ("multi notices with no expected lot count",
                 c.get("multi_no_lots")),
            ], total), "the lot count is the checksum extraction is judged against"),
        ]

    elif key in ("ocr", "ocr_ok"):
        total = int(_count_query(
            "MATCH (d:Document) WHERE d.markdown IS NOT NULL AND d.markdown <> '' "
            "RETURN count(d) AS n").get("n") or 0)
        engine_rows = run_read_query(
            "MATCH (d:Document) WHERE d.markdown IS NOT NULL AND d.markdown <> '' "
            "RETURN coalesce(d.markdown_model,'unknown') AS t, count(*) AS n "
            "ORDER BY n DESC", max_rows=12, timeout=30.0)
        health = _count_query("""
            MATCH (d:Document) WHERE d.ocr_health_score IS NOT NULL
            RETURN sum(CASE WHEN d.ocr_health_score = 100 THEN 1 ELSE 0 END) AS clean,
                   sum(CASE WHEN d.ocr_health_score < 100
                             AND d.ocr_health_score >= 60 THEN 1 ELSE 0 END) AS mid,
                   sum(CASE WHEN d.ocr_health_score < 60 THEN 1 ELSE 0 END) AS bad
        """)
        flags = run_read_query("""
            MATCH (d:Document) WHERE size(coalesce(d.ocr_health_flags,[])) > 0
            UNWIND d.ocr_health_flags AS f
            RETURN f AS t, count(*) AS n ORDER BY n DESC
        """, max_rows=12, timeout=30.0)
        # ink_uncovered_ratio is the page TOTAL; the missing-region flag fires on
        # the largest contiguous patch. They are different measures and a doc can
        # carry 20% scattered without being flagged, so the flagged count is read
        # from the flag itself rather than re-derived from the ratio.
        ink = _count_query("""
            MATCH (d:Document) WHERE d.ink_uncovered_ratio IS NOT NULL
            RETURN count(d) AS measured,
                   sum(CASE WHEN d.ink_uncovered_ratio < 0.05 THEN 1 ELSE 0 END) AS tight,
                   sum(CASE WHEN 'missing-region' IN coalesce(d.ocr_health_flags,[])
                            THEN 1 ELSE 0 END) AS flagged
        """)
        review = _count_query("""
            MATCH (d:Document) WHERE d.markdown IS NOT NULL AND d.markdown <> ''
            RETURN sum(CASE WHEN d.markdown_verified_at IS NOT NULL
                        AND d.markdown_quality = 'good' THEN 1 ELSE 0 END) AS good,
                   sum(CASE WHEN d.markdown_verified_at IS NOT NULL
                        AND d.markdown_quality = 'bad' THEN 1 ELSE 0 END) AS bad,
                   sum(CASE WHEN d.markdown_verified_at IS NULL THEN 1 ELSE 0 END) AS pending,
                   sum(CASE WHEN d.markdown_reextracted_at IS NOT NULL
                            THEN 1 ELSE 0 END) AS reextracted
        """)
        out["panels"] = [
            _panel("Engine", _rows([(r["t"], r["n"]) for r in engine_rows], total),
                   "which parser produced the text on file"),
            _panel("Health", _rows([
                ("clean (100)", health.get("clean")),
                ("60–99", health.get("mid")),
                ("below 60", health.get("bad")),
            ], total), "score after every failure-mode penalty"),
            _panel("Failures", _rows(
                [(r["t"], r["n"]) for r in flags], total,
                href=lambda f: f"#stage=markdown&group=notice&status=all"
                               f"&notice_type=all&flags={f}"),
                "click a failure to open that queue"),
            _panel("Missing content", _rows([
                ("checked for missing content", ink.get("measured")),
                ("under 5% of ink unread in total", ink.get("tight")),
                ("flagged: one solid patch never read", ink.get("flagged")),
                ("never checked", total - int(ink.get("measured") or 0)),
            ], total),
                   "the flag fires on the largest single gap, not the page total — "
                   "scattered slivers are bbox slop, one solid patch is lost content"),
            _panel("Review", _rows([
                ("accepted", review.get("good")),
                ("marked bad", review.get("bad")),
                ("not yet reviewed", review.get("pending")),
                ("re-extracted by a reviewer", review.get("reextracted")),
            ], total), ""),
        ]

    elif key in ("extracted", "extract_ok"):
        total = int(_count_query(
            "MATCH (d:Document) WHERE d.extraction_json IS NOT NULL "
            "RETURN count(d) AS n").get("n") or 0)
        status = run_read_query(
            "MATCH (d:Document) WHERE d.extraction_json IS NOT NULL "
            "RETURN coalesce(d.extraction_review_status,'pending') AS t, "
            "count(*) AS n ORDER BY n DESC", max_rows=10, timeout=30.0)
        score = _count_query("""
            MATCH (d:Document) WHERE d.extraction_score IS NOT NULL
            RETURN sum(CASE WHEN d.extraction_score >= 90 THEN 1 ELSE 0 END) AS top,
                   sum(CASE WHEN d.extraction_score >= 70
                             AND d.extraction_score < 90 THEN 1 ELSE 0 END) AS mid,
                   sum(CASE WHEN d.extraction_score < 70 THEN 1 ELSE 0 END) AS low,
                   sum(CASE WHEN d.extraction_stale_at IS NOT NULL THEN 1 ELSE 0 END)
                       AS stale
        """)
        out["panels"] = [
            _panel("Extraction score", _rows([
                ("90 or above", score.get("top")),
                ("70–89", score.get("mid")),
                ("below 70 — worth a look", score.get("low")),
            ], total), "validators.py score over the stored entities"),
            _panel("Review", _rows(
                [(r["t"], r["n"]) for r in status], total,
                href=lambda s: f"#stage=extraction&group=notice&status={s}"),
                "click a status to open that queue"),
            _panel("Rerun needed", _rows([
                ("markdown changed since extraction", score.get("stale")),
            ], total), "their entities were read off text that has been replaced"),
        ] + _entity_coverage_panels(sample)

    elif key == "resolved":
        total = int(_count_query(
            "MATCH (d:Document) WHERE d.bank_canonical IS NOT NULL "
            "RETURN count(d) AS n").get("n") or 0)
        state = _count_query(
            "MATCH (s:PipelineState {key:'entity_resolution'}) "
            "RETURN s.raw_values AS raw, s.entities AS entities, "
            "       s.merged_spellings AS merged, s.proposals_open AS proposals, "
            "       s.proposals_json AS proposals_json")
        top = run_read_query(
            "MATCH (d:Document) WHERE d.bank_canonical IS NOT NULL "
            "RETURN d.bank_canonical AS t, count(*) AS n ORDER BY n DESC LIMIT 12",
            max_rows=12, timeout=30.0)
        spelt = run_read_query(
            """
            MATCH (d:Document)
            WHERE d.bank_canonical IS NOT NULL AND d.bank_name_raw IS NOT NULL
              AND d.bank_name_raw <> d.bank_canonical
            RETURN d.bank_canonical AS t, count(DISTINCT d.bank_name_raw) AS n
            ORDER BY n DESC LIMIT 10
            """, max_rows=10, timeout=30.0)
        proposals: list[dict] = []
        try:
            import json as _json
            proposals = _json.loads(state.get("proposals_json") or "[]")
        except (TypeError, ValueError):
            proposals = []
        out["panels"] = [
            _panel("Lenders", _rows([
                ("name strings extracted", state.get("raw")),
                ("distinct lenders after resolution", state.get("entities")),
                ("spellings absorbed", state.get("merged")),
                ("notices carrying a resolved lender", total),
            ], int(state.get("raw") or 0) or total),
                   "only exact matches after case, punctuation and legal form "
                   "are normalised away — nothing merges on resemblance"),
            _panel("Awaiting a human", _rows(
                [(f"{p.get('a')}   vs  {p.get('b')}", p.get("score"))
                 for p in proposals[:20]], 100),
                   f"{len(proposals)} pair(s) too similar to ignore and too "
                   "risky to merge automatically — the number shown is the "
                   "similarity score, not a count"),
            _panel("Most frequent lenders",
                   _rows([(r["t"], r["n"]) for r in top], total), ""),
            _panel("Most spelling variants", _rows(
                [(r["t"], r["n"]) for r in spelt], total),
                   "how many different spellings each lender arrived under"),
        ] + _place_panels()

    elif key == "resolve_ok":
        out["panels"] = _resolution_review_panels()

    return out


def _place_panels() -> list[dict]:
    """How far each property got down the revenue hierarchy.

    Places resolve against an authority the bank names never had — the
    gazetteer — so the interesting number is not how many merged but how deep
    each one reached, and why the rest stopped where they did.
    """
    counts = _count_query("""
        MATCH (p:AuctionProperty)
        RETURN count(p) AS total,
               sum(CASE WHEN p.revenue_district IS NOT NULL THEN 1 ELSE 0 END) AS d,
               sum(CASE WHEN p.revenue_taluk    IS NOT NULL THEN 1 ELSE 0 END) AS t,
               sum(CASE WHEN p.revenue_village  IS NOT NULL THEN 1 ELSE 0 END) AS v,
               sum(CASE WHEN p.place_portal_conflict THEN 1 ELSE 0 END) AS portal,
               sum(CASE WHEN p.place_notice_conflict THEN 1 ELSE 0 END) AS notice
    """)
    total = int(counts.get("total") or 0)
    stops = run_read_query(
        """
        MATCH (p:AuctionProperty) WHERE p.place_village_status IS NOT NULL
          AND p.revenue_village IS NULL
        RETURN p.place_village_status AS t, count(*) AS n ORDER BY n DESC
        """, max_rows=12, timeout=30.0)
    # Plain-English labels: the stored values are status codes, and a reviewer
    # should not have to know that "taluk-has-no-villages" is a gap in the
    # reference data rather than a bad read.
    said = {
        "unmatched": "village named, not found in its taluk",
        "absent": "no village named in the notice",
        "no-parent-taluk": "village named but no taluk to place it in",
        "taluk-has-no-villages": "taluk keeps no revenue villages (urban)",
        "names-a-taluk": "village field repeats the taluk name",
    }
    return [
        _panel("Places matched to the revenue record", _rows([
            ("district", counts.get("d")),
            ("taluk", counts.get("t")),
            ("village", counts.get("v")),
        ], total),
               "read bottom-up: a taluk names its own district, so a misspelt "
               "or pre-2019 district is corrected by the taluk beneath it"),
        _panel("Where the village stops", _rows(
            [(said.get(r["t"], r["t"]), r["n"]) for r in stops], total),
               "nothing is guessed — a wrong place is worse than a missing "
               "one, because a missing one is visible"),
        _panel("Disagreements", _rows([
            ("notice district vs its own taluk", counts.get("notice")),
            ("portal city vs the resolved district", counts.get("portal")),
        ], total),
               "the portal never supplies an answer; it is kept only to "
               "disagree, which is how an extraction error shows up"),
    ]


# ── Entity-resolution review ────────────────────────────────────────────────
#
# The resolvers stop where a rule cannot decide, and what they leave behind is
# corpus-shaped: a lookalike lender pair touches every notice naming either
# spelling, a district-conflict pattern covers every notice writing that
# district over that taluk. So the review queues hold *facts to settle*, not
# documents to walk, and a verdict is stored as a (:ResolutionDecision) node
# the resolvers consult on every subsequent run — approved merges apply
# forever, rejected pairs never come back, aliases teach the lookup. The
# queues below also filter decided keys at read time, so a verdict empties its
# row immediately rather than after the next resolution run.


def _load_decisions() -> list[dict]:
    import json as _json
    rows = run_read_query(
        """
        MATCH (r:ResolutionDecision)
        RETURN r.key AS key, r.kind AS kind, r.verdict AS verdict,
               r.payload_json AS payload_json
        """, max_rows=5000, timeout=30.0)
    out = []
    for r in rows:
        try:
            payload = _json.loads(r.get("payload_json") or "{}")
        except (TypeError, ValueError):
            payload = {}
        out.append({"key": r["key"], "kind": r["kind"],
                    "verdict": r["verdict"], "payload": payload})
    return out


def _village_candidates(taluks: list[str]) -> dict[str, list[str]]:
    """Official village names per taluk, for suggesting alias targets."""
    rows = run_read_query(
        """
        MATCH (v:RevenueVillage)-[:IN_TALUK]->(t:Taluk)
        WHERE t.name IN $taluks
        RETURN t.name AS taluk, collect(v.name) AS villages
        """, {"taluks": taluks}, max_rows=len(taluks) or 1, timeout=30.0)
    return {r["taluk"]: r["villages"] for r in rows}


def resolution_review() -> dict:
    """The three queues a human works through, with evidence on every row.

    Ranked by how much one verdict fixes: a bank pair settles a lender
    identity, a conflict pattern settles every notice writing that district
    over that taluk, a village row settles every property naming that string
    in that taluk.
    """
    import json as _json

    from pipeline.place_resolution import normalize_place
    from pipeline.resolution_review import (
        bank_pair_key, district_conflict_key, settled_conflicts,
        skipped_villages, village_alias_key, village_aliases,
    )

    decisions = _load_decisions()
    ruled_pairs = {d["key"] for d in decisions if d["kind"] == "bank-merge"}
    settled = settled_conflicts(decisions)
    aliased = set(village_aliases(decisions))
    skipped = skipped_villages(decisions)

    # Bank lookalike pairs — stored by the resolver, already excluding pairs
    # decided before its last run; the key filter catches ones decided since.
    state = _count_query(
        "MATCH (s:PipelineState {key:'entity_resolution'}) "
        "RETURN s.proposals_json AS pj")
    try:
        proposals = _json.loads(state.get("pj") or "[]")
    except (TypeError, ValueError):
        proposals = []
    pairs = [p for p in proposals
             if bank_pair_key(p["a"], p["b"]) not in ruled_pairs]
    names = sorted({p["a"] for p in pairs} | {p["b"] for p in pairs})
    examples: dict[str, list[str]] = {}
    if names:
        for r in run_read_query(
                """
                MATCH (d:Document) WHERE d.bank_canonical IN $names
                RETURN d.bank_canonical AS name,
                       collect(d.filename)[0..2] AS files
                """, {"names": names}, max_rows=len(names), timeout=30.0):
            examples[r["name"]] = r["files"]
    bank_pairs = [{
        "score": p["score"], "a": p["a"], "b": p["b"],
        "a_count": p["a_count"], "b_count": p["b_count"],
        "a_files": examples.get(p["a"], []),
        "b_files": examples.get(p["b"], []),
    } for p in pairs]

    # Branch lookalike pairs — same design as lenders, scoped per bank.
    from pipeline.resolution_review import filter_branch_proposals
    bstate = _count_query(
        "MATCH (s:PipelineState {key:'branch_resolution'}) "
        "RETURN s.proposals_json AS pj")
    try:
        branch_props = _json.loads(bstate.get("pj") or "[]")
    except (TypeError, ValueError):
        branch_props = []
    branch_pairs = filter_branch_proposals(branch_props, decisions)

    # District-conflict patterns — grouped, one row per (raw spelling, taluk).
    pstate = _count_query(
        "MATCH (s:PipelineState {key:'place_resolution'}) "
        "RETURN s.conflicts_json AS cj")
    try:
        conflicts = _json.loads(pstate.get("cj") or "[]")
    except (TypeError, ValueError):
        conflicts = []
    grouped: dict[tuple, dict] = {}
    for c in conflicts:
        key = district_conflict_key(c.get("raw_district") or "",
                                    c.get("taluk") or "")
        if key in settled:
            continue
        gk = (c.get("raw_district"), c.get("taluk"), c.get("resolved_district"))
        g = grouped.setdefault(gk, {
            "raw_district": c.get("raw_district"), "taluk": c.get("taluk"),
            "resolved_district": c.get("resolved_district"),
            "count": 0, "auction_ids": []})
        g["count"] += 1
        if len(g["auction_ids"]) < 3:
            g["auction_ids"].append(c.get("auction_id"))
    district_conflicts = sorted(grouped.values(), key=lambda g: -g["count"])

    # Unmatched villages — grouped by (string, taluk), with the taluk's
    # closest official names as candidate alias targets so the reviewer picks
    # rather than types.
    rows = run_read_query(
        """
        MATCH (p:AuctionProperty)
        WHERE p.place_village_status = 'unmatched'
          AND p.village IS NOT NULL AND p.revenue_taluk IS NOT NULL
        RETURN p.village AS village, p.revenue_taluk AS taluk,
               p.revenue_district AS district,
               count(*) AS n, collect(p.auction_id)[0..3] AS auction_ids
        ORDER BY n DESC
        """, max_rows=2000, timeout=60.0)
    open_rows = [r for r in rows
                 if village_alias_key(r["village"], r["taluk"]) not in aliased
                 and normalize_place(r["village"]) not in skipped]
    open_rows = open_rows[:60]
    pools = _village_candidates(sorted({r["taluk"] for r in open_rows}))
    try:
        from rapidfuzz import fuzz
        def top3(raw: str, taluk: str) -> list[dict]:
            nv = normalize_place(raw)
            scored = sorted(
                ((fuzz.ratio(nv, normalize_place(v)), v)
                 for v in pools.get(taluk, [])), reverse=True)[:3]
            return [{"name": v, "score": round(float(s), 1)}
                    for s, v in scored if s >= 55]
    except ImportError:
        def top3(raw: str, taluk: str) -> list[dict]:
            return []
    unmatched_villages = [{
        "village": r["village"], "taluk": r["taluk"],
        "district": r["district"], "count": r["n"],
        "auction_ids": r["auction_ids"],
        "candidates": top3(r["village"], r["taluk"]),
    } for r in open_rows]

    lot_matches = _lot_match_candidates(decisions)

    return {
        "bank_pairs": bank_pairs,
        "branch_pairs": branch_pairs,
        "district_conflicts": district_conflicts,
        "unmatched_villages": unmatched_villages,
        "lot_matches": lot_matches,
        "decided": len(decisions),
        "open": (len(bank_pairs) + len(branch_pairs)
                 + len(district_conflicts) + len(unmatched_villages)
                 + len(lot_matches)),
    }


#: Cap on rows in one queue load — this queue is a fully-computed exact
#: count (not a corpus-wide fuzzy proposal set that could run unbounded), so
#: 200 comfortably covers everything seen live (160) with headroom, while
#: still bounding the query if the backlog ever grows past what one page
#: should show at once.
_LOT_MATCH_LIMIT = 200


def _lot_match_candidates(decisions: list[dict]) -> list[dict]:
    """Listings on a multi-lot notice `pipeline/lot_resolution.py` could not
    place on its own — the queue a human works through.

    A live query, not a stored proposal set (unlike bank/branch lookalikes):
    the resolver's `reserve_price_num` join is exact and cheap to recompute
    on every load, and "still ambiguous" is already the WHOLE definition of
    the queue (`resolved_lot_key IS NULL` on a multi-lot notice) — there is
    no separate fuzzy-candidate-generation step to cache. The exception is
    a decision that hasn't been applied to the graph yet (see
    `decided_lot_matches`): approving a lot, or rejecting all of them,
    leaves `resolved_lot_key` untouched until the next "Apply my
    decisions" run — a row must still drop off the queue the instant it's
    decided, so it's filtered here in Python instead.

    Each row carries the SAME evidence the resolver itself compared —
    reserve price and borrower name — on both the listing and every
    candidate lot, so a human sees exactly what the rule saw and why it
    couldn't decide, not just a bare "pick one". It also carries the notice
    image (`public_url`), this listing's own eauctionsindia link
    (`listing_url`), and every other AuctionProperty sharing the same
    notice (`db_properties`, each with its own eauctionsindia link) — a
    9-lot notice the portal only scraped once should read as "1 property in
    our DB", not be confused with the notice's own lot count.
    """
    from pipeline.lot_resolution import resolve_lot
    from pipeline.resolution_review import decided_lot_matches

    skipped = decided_lot_matches(decisions)

    rows = run_read_query(
        """
        MATCH (p:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)-[:HAS_LOT]->(l:Lot)
        WITH p, d, count(l) AS lot_count
        WHERE lot_count > 1 AND p.resolved_lot_key IS NULL
          AND NOT p.auction_id IN $skipped
        RETURN p.auction_id AS auction_id, p.title AS title, p.url AS listing_url,
               d.file_path AS file_path, d.public_url AS public_url,
               p.reserve_price_num AS reserve, lot_count,
               [(p)-[:HAS_BORROWER]->(b:Borrower) | b.name][0] AS borrower
        ORDER BY lot_count, auction_id
        LIMIT $limit
        """, {"limit": _LOT_MATCH_LIMIT, "skipped": sorted(skipped)},
        max_rows=_LOT_MATCH_LIMIT, timeout=60.0)
    if not rows:
        return []

    file_paths = sorted({r["file_path"] for r in rows})
    lot_rows = run_read_query(
        """
        MATCH (d:Document)-[:HAS_LOT]->(l:Lot)
        WHERE d.file_path IN $paths
        OPTIONAL MATCH (l)-[:OFFERED_IN]->(au:Auction)
        OPTIONAL MATCH (l)-[e:HAS_EXTENT]->(m:Measurement) WHERE e.is_headline
        RETURN d.file_path AS file_path, l.lot_key AS lot_key,
               au.reserve_price_num AS reserve, m.sqft_norm AS sqft,
               l.address AS address,
               [(l)-[:HAS_PARTY|TITLE_HELD_BY]->(b:Borrower) | b.name] AS borrowers
        """, {"paths": file_paths}, max_rows=5000, timeout=60.0)
    lots_by_fp: dict[str, list[dict]] = {}
    for r in lot_rows:
        lots_by_fp.setdefault(r["file_path"], []).append({
            "lot_key": r["lot_key"], "reserve": r["reserve"], "sqft": r["sqft"],
            "address": r["address"], "borrowers": [b for b in (r["borrowers"] or []) if b],
        })

    # Every AuctionProperty that shares this notice's Document — not just
    # the ones with >1 Lot. Answers "how many properties do we actually
    # have for this notice" directly: a 9-lot notice with one scraped
    # listing shows a db_properties list of length 1, not 9.
    sib_rows = run_read_query(
        """
        MATCH (d:Document)-[:HAS_DOCUMENT]-(sib:AuctionProperty)
        WHERE d.file_path IN $paths
        RETURN d.file_path AS file_path, sib.auction_id AS auction_id,
               sib.title AS title, sib.url AS url,
               sib.reserve_price_num AS reserve
        """, {"paths": file_paths}, max_rows=5000, timeout=60.0)
    sibs_by_fp: dict[str, list[dict]] = {}
    for r in sib_rows:
        sibs_by_fp.setdefault(r["file_path"], []).append({
            "auction_id": r["auction_id"], "title": r["title"],
            "url": r["url"], "reserve": r["reserve"],
        })

    out = []
    for r in rows:
        candidates = lots_by_fp.get(r["file_path"], [])
        verdict = resolve_lot(
            listing_reserve=r["reserve"], listing_borrower=r["borrower"],
            candidates=[{"lot_key": c["lot_key"], "reserve": c["reserve"],
                        "borrowers": c["borrowers"]} for c in candidates])
        out.append({
            "auction_id": r["auction_id"], "title": r["title"],
            "listing_url": r["listing_url"], "public_url": r["public_url"],
            "reserve": r["reserve"], "borrower": r["borrower"],
            "lot_count": r["lot_count"],
            "reason": verdict["reason"],
            "candidates": [{
                "lot_key": c["lot_key"], "reserve": c["reserve"],
                "sqft": round(c["sqft"], 1) if c["sqft"] is not None else None,
                "address": c["address"], "borrowers": c["borrowers"],
            } for c in candidates],
            "db_properties": sibs_by_fp.get(r["file_path"], []),
        })
    return out


def record_resolution_decision(kind: str, payload: dict, verdict: str,
                               by_email: str) -> dict:
    """Store one human verdict as a (:ResolutionDecision) node.

    The key is always derived from the kind and payload — never accepted from
    the caller — so a decision can only land on the strings it names. An
    approved village alias is checked against the gazetteer first, and an
    approved lot-match against the listing's own document: either one, a bad
    value here must fail loudly rather than invent a place or a lot downstream.
    """
    import json as _json

    from pipeline.resolution_review import APPROVED, REJECTED, decision_key

    if verdict not in (APPROVED, REJECTED):
        raise ValueError(f"verdict must be approved or rejected, got {verdict!r}")
    try:
        key = decision_key(kind, payload)
    except KeyError as e:
        raise ValueError(f"payload for {kind!r} is missing field {e}")

    if kind == "village-alias" and verdict == APPROVED:
        hit = _count_query(
            """
            MATCH (v:RevenueVillage {name: $target})-[:IN_TALUK]->
                  (t:Taluk {name: $taluk})
            RETURN count(v) AS n
            """, {"target": payload.get("target"),
                  "taluk": payload.get("taluk")})
        if not int(hit.get("n") or 0):
            raise ValueError(
                f"{payload.get('target')!r} is not a revenue village of "
                f"{payload.get('taluk')!r} — the alias would point nowhere")

    if kind == "lot-match" and verdict == APPROVED:
        # A picked lot_key must actually be on THIS listing's document — the
        # review UI only ever offers lots from the listing's own candidate
        # list, but the endpoint accepts arbitrary payloads, and a bad key
        # here would silently point `resolved_lot_key` at nothing.
        hit = _count_query(
            """
            MATCH (p:AuctionProperty {auction_id: $auction_id})
                  -[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->
                  (l:Lot {lot_key: $lot_key})
            RETURN count(l) AS n
            """, {"auction_id": payload.get("auction_id"),
                  "lot_key": payload.get("lot_key")})
        if not int(hit.get("n") or 0):
            raise ValueError(
                f"{payload.get('lot_key')!r} is not a lot on "
                f"{payload.get('auction_id')!r}'s sale notice")

    run_query(
        """
        MERGE (r:ResolutionDecision {key: $key})
        SET r.kind = $kind, r.verdict = $verdict,
            r.payload_json = $payload, r.decided_at = datetime(),
            r.decided_by = $by
        """,
        {"key": key, "kind": kind, "verdict": verdict,
         "payload": _json.dumps(payload, ensure_ascii=False), "by": by_email})
    return {"key": key, "kind": kind, "verdict": verdict}


def undo_resolution_decision(kind: str, payload: dict) -> dict:
    """Delete a stored verdict so the question reopens on the next run."""
    from pipeline.resolution_review import decision_key
    try:
        key = decision_key(kind, payload)
    except KeyError as e:
        raise ValueError(f"payload for {kind!r} is missing field {e}")
    rows = run_query(
        "MATCH (r:ResolutionDecision {key: $key}) DELETE r RETURN count(r) AS n",
        {"key": key})
    n = int(rows[0].get("n") or 0) if rows else 0
    return {"key": key, "deleted": bool(n)}


def _resolution_review_panels() -> list[dict]:
    """The resolve_ok stage page: what still blocks review-complete, and the
    ledger of verdicts already banked."""
    c = _count_query("""
        MATCH (d:Document)
        RETURN sum(CASE WHEN d.entity_resolved_at IS NOT NULL
                        THEN 1 ELSE 0 END) AS resolved,
               sum(CASE WHEN d.bank_attention THEN 1 ELSE 0 END) AS bank_att,
               sum(CASE WHEN d.place_attention THEN 1 ELSE 0 END) AS place_att,
               sum(CASE WHEN d.branch_attention THEN 1 ELSE 0 END) AS branch_att,
               sum(CASE WHEN d.entity_resolved_at IS NOT NULL
                        AND d.bank_attention IS NULL
                        AND d.place_attention IS NULL
                        AND d.branch_attention IS NULL
                        THEN 1 ELSE 0 END) AS clean
    """)
    ledger = run_read_query(
        """
        MATCH (r:ResolutionDecision)
        RETURN r.kind + ' — ' + r.verdict AS t, count(*) AS n ORDER BY n DESC
        """, max_rows=12, timeout=30.0)
    queues = resolution_review()
    total = int(c.get("resolved") or 0)
    return [
        _panel("Review state", _rows([
            ("notices with both resolvers run", c.get("resolved")),
            ("clean — no open question", c.get("clean")),
            ("waiting on a lender verdict", c.get("bank_att")),
            ("waiting on a place verdict", c.get("place_att")),
            ("waiting on a branch verdict", c.get("branch_att")),
        ], total),
               "a notice is review-complete when nothing about it is still "
               "an open question; verdicts below shrink these numbers on the "
               "next resolver run"),
        _panel("Open questions", _rows([
            ("lender lookalike pairs", len(queues["bank_pairs"])),
            ("branch lookalike pairs", len(queues["branch_pairs"])),
            ("district conflict patterns", len(queues["district_conflicts"])),
            ("unmatched village strings", len(queues["unmatched_villages"])),
            ("lot matches to review", len(queues["lot_matches"])),
        ], max(queues["open"], 1)),
               "each row on the review queue settles every notice it touches"),
        _panel("Verdicts banked", _rows(
            [(r["t"], r["n"]) for r in ledger], max(
                sum(r["n"] for r in ledger), 1)),
               "stored permanently — re-runs apply them before proposing "
               "anything"),
    ]


# ── Apply decisions (re-run the resolvers) ──────────────────────────────────
#
# A verdict is applied by the resolvers, so between a review session and the
# next run the queue is ahead of the graph. The apply endpoint closes that gap
# on demand: it re-runs both resolvers in the API process (they talk to Neo4j
# over the HTTPS query API with the same credentials the API already holds).
# Status lives on a (:PipelineState {key:'resolution_apply'}) node — not in
# process memory — so any worker can answer "is it still running?".

_APPLY_STALE_S = 15 * 60.0


def resolution_apply_status() -> dict:
    import time as _time
    row = _count_query(
        "MATCH (s:PipelineState {key:'resolution_apply'}) "
        "RETURN s.status AS status, s.started_ts AS started_ts, "
        "       s.finished_ts AS finished_ts, s.by AS by, "
        "       s.summary_json AS summary_json, s.error AS error")
    status = row.get("status") or "never-run"
    started = float(row.get("started_ts") or 0)
    # A run that started long ago and never finished is a crashed worker, not
    # a busy one — report it as such rather than blocking apply forever.
    if status == "running" and _time.time() - started > _APPLY_STALE_S:
        status = "stale"
    return {"status": status, "started_ts": started or None,
            "finished_ts": row.get("finished_ts"), "by": row.get("by"),
            "summary_json": row.get("summary_json"),
            "error": row.get("error")}


def start_resolution_apply(by_email: str) -> dict:
    """Claim the apply lock; raises if a run is genuinely in progress."""
    import time as _time
    current = resolution_apply_status()
    if current["status"] == "running":
        raise RuntimeError("a resolver run is already in progress")
    run_query(
        """
        MERGE (s:PipelineState {key:'resolution_apply'})
        SET s.status = 'running', s.started_ts = $ts, s.by = $by,
            s.finished_ts = NULL, s.error = NULL
        """, {"ts": _time.time(), "by": by_email})
    return {"status": "running", "by": by_email}


def run_resolution_apply() -> None:
    """The background job: both resolvers, then the outcome — success or
    failure — written where the UI can read it."""
    import json as _json
    import time as _time
    try:
        from scripts.resolve_bank_names import run as run_banks
        from scripts.resolve_branches import run as run_branches
        from scripts.resolve_lots import run as run_lots
        from scripts.resolve_places import run as run_places
        # Branches after banks: their scope is d.bank_canonical, which the
        # lender pass may have just rewritten. Lots is independent of both —
        # order doesn't matter, it only reads reserve price and borrower name.
        summary = {"banks": run_banks(), "branches": run_branches(),
                   "places": run_places(), "lots": run_lots()}
        run_query(
            """
            MERGE (s:PipelineState {key:'resolution_apply'})
            SET s.status = 'done', s.finished_ts = $ts,
                s.summary_json = $summary, s.error = NULL
            """, {"ts": _time.time(),
                  "summary": _json.dumps(summary, ensure_ascii=False)})
    except Exception as e:                                  # noqa: BLE001
        # The worker survives; the failure is data for the status endpoint.
        run_query(
            """
            MERGE (s:PipelineState {key:'resolution_apply'})
            SET s.status = 'error', s.finished_ts = $ts, s.error = $err
            """, {"ts": _time.time(), "err": f"{type(e).__name__}: {e}"[:500]})
