"""
api/review/queries.py
---------------------
Cypher gateway for the enrichment review queue.
"""
from __future__ import annotations

from typing import Literal

from api.neo4j_client import run_query, run_read_query
from api.review.markdown_match import property_offset_in_notice


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


def list_queue(
    status: ReviewStatus = "pending",
    q: str | None = None,
    page: int = 1,
    size: int = 50,
    date_from: str | None = None,
    date_to: str | None = None,
    notice_type: NoticeTypeFilter | None = None,
) -> dict:
    """Return a page of properties whose description came from a notice
    extraction (or a human edit), filtered by review status.

    Pending = description_source IN ['notice','human'] AND NOT description_verified.
    Verified = description_verified = true AND description_source = 'notice'.
    Edited = description_verified = true AND description_source = 'human'.

    Optional date_from / date_to filter on auction_start_dt (ISO date strings,
    YYYY-MM-DD). Property is included if its auction_start_dt falls in the
    [date_from, date_to] window (inclusive). Properties with a null
    auction_start_dt are excluded when either bound is set.
    """
    page = max(1, int(page))
    size = max(1, min(200, int(size)))
    skip = (page - 1) * size

    where = ["a.description_source IN ['notice', 'human']", "a.description IS NOT NULL"]
    if status == "pending":
        where.append("coalesce(a.description_verified, false) = false")
    elif status == "verified":
        where.append("coalesce(a.description_verified, false) = true")
        where.append("a.description_source = 'notice'")
    elif status == "edited":
        where.append("coalesce(a.description_verified, false) = true")
        where.append("a.description_source = 'human'")
    # "all" → no extra filter

    params: dict = {"skip": skip, "size": size}
    if q:
        where.append(
            "(toLower(coalesce(a.title, '')) CONTAINS toLower($q) "
            "OR toLower(coalesce(b.name, '')) CONTAINS toLower($q))"
        )
        params["q"] = q
    if date_from:
        where.append("a.auction_start_dt IS NOT NULL AND date(a.auction_start_dt) >= date($date_from)")
        params["date_from"] = date_from
    if date_to:
        where.append("a.auction_start_dt IS NOT NULL AND date(a.auction_start_dt) <= date($date_to)")
        params["date_to"] = date_to

    nt_clause = _notice_type_clause(notice_type, alias="d")
    if nt_clause:
        where.append(nt_clause)

    where_clause = " AND ".join(where)

    cypher = f"""
        MATCH (a:AuctionProperty)
        OPTIONAL MATCH (a)-[:HAS_BORROWER]->(b:Borrower)
        OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(d:Document)
        WITH a, collect(DISTINCT b.name) AS borrowers,
             collect(DISTINCT d) AS docs
        WITH a, borrowers, docs, head(docs) AS d
        WHERE {where_clause}
        RETURN a.auction_id                AS auction_id,
               a.title                     AS title,
               borrowers                   AS borrowers,
               a.reserve_price_num         AS reserve_price,
               a.description_completeness  AS completeness,
               a.description_source        AS source,
               coalesce(a.description_verified, false) AS verified,
               a.description_verified_at   AS verified_at,
               a.description_verified_by   AS verified_by,
               d.notice_type               AS notice_type,
               (d.public_url IS NOT NULL AND d.public_url <> '') AS has_pdf
        ORDER BY verified ASC,
                 coalesce(a.description_completeness, 0.0) ASC,
                 a.auction_id ASC
        SKIP $skip LIMIT $size
    """
    rows = run_read_query(cypher, params, max_rows=size)

    # Total count.  When a notice_type filter is active, we must join Document
    # to apply it; otherwise strip d. predicates as before (the count query
    # in the no-filter path doesn't pull docs for performance).
    if notice_type:
        count_cypher = f"""
            MATCH (a:AuctionProperty)
            OPTIONAL MATCH (a)-[:HAS_BORROWER]->(b:Borrower)
            OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(d:Document)
            WITH a, collect(DISTINCT b.name) AS borrowers,
                 collect(DISTINCT d) AS docs
            WITH a, borrowers, head(docs) AS d
            WHERE {where_clause}
            RETURN count(a) AS total
        """
    else:
        count_where = [w for w in where if "d." not in w]
        count_cypher = f"""
            MATCH (a:AuctionProperty)
            OPTIONAL MATCH (a)-[:HAS_BORROWER]->(b:Borrower)
            WITH a, collect(DISTINCT b.name) AS borrowers
            WHERE {' AND '.join(count_where)}
            RETURN count(a) AS total
        """
    count_rows = run_read_query(count_cypher, params, max_rows=1)
    total = count_rows[0]["total"] if count_rows else 0

    return {"page": page, "size": size, "total": total, "rows": rows}


def list_notice_queue(
    status: ReviewStatus = "pending",
    q: str | None = None,
    page: int = 1,
    size: int = 50,
    date_from: str | None = None,
    date_to: str | None = None,
    notice_type: NoticeTypeFilter | None = None,
) -> dict:
    """Return a page of sales notices (Documents), each carrying the list of
    AuctionProperty rows extracted from it.

    Lets reviewers close 5–7 listings of a multi-property notice in one sitting
    instead of jumping back to the queue after every verify.

    Status semantics at the notice level:
    - pending: notice has at least one property still pending review
    - verified: every property under the notice has been verified
    - edited: at least one property under the notice was human-edited
    - all: every notice that backs any reviewable property

    date_from / date_to filter linked properties by auction_start_dt. A notice
    is surfaced if any property survives the date filter; aggregate counts
    (total/pending/verified/edited) are computed over surviving properties only,
    so filtered totals reflect what the reviewer can actually act on.
    """
    page = max(1, int(page))
    size = max(1, min(200, int(size)))
    skip = (page - 1) * size

    if status == "pending":
        notice_filter = "pending_count > 0"
    elif status == "verified":
        notice_filter = "pending_count = 0"
    elif status == "edited":
        notice_filter = "edited_count > 0"
    else:
        notice_filter = "true"

    params: dict = {"skip": skip, "size": size}
    search_filter = "true"
    if q:
        params["q"] = q
        search_filter = (
            "(toLower(coalesce(d.filename, '')) CONTAINS toLower($q) "
            "OR ANY(p IN properties WHERE "
            "  toLower(coalesce(p.title, '')) CONTAINS toLower($q) "
            "  OR ANY(bb IN p.borrowers WHERE toLower(coalesce(bb, '')) CONTAINS toLower($q))))"
        )

    date_predicate = ""
    if date_from:
        date_predicate += " AND a.auction_start_dt IS NOT NULL AND date(a.auction_start_dt) >= date($date_from)"
        params["date_from"] = date_from
    if date_to:
        date_predicate += " AND a.auction_start_dt IS NOT NULL AND date(a.auction_start_dt) <= date($date_to)"
        params["date_to"] = date_to
    nt_clause = _notice_type_clause(notice_type, alias="d")
    if nt_clause:
        date_predicate += f" AND {nt_clause}"

    cypher = f"""
        MATCH (d:Document)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
        WHERE a.description_source IN ['notice', 'human']
          AND a.description IS NOT NULL
          {date_predicate}
        OPTIONAL MATCH (a)-[:HAS_BORROWER]->(b:Borrower)
        WITH d, a, collect(DISTINCT b.name) AS borrowers
        WITH d, collect({{
                auction_id:    a.auction_id,
                title:         a.title,
                borrowers:     borrowers,
                reserve_price: a.reserve_price_num,
                completeness:  a.description_completeness,
                source:        a.description_source,
                verified:      coalesce(a.description_verified, false),
                verified_at:   a.description_verified_at,
                verified_by:   a.description_verified_by
             }}) AS properties
        WITH d, properties,
             size(properties) AS total_count,
             size([p IN properties WHERE p.verified = false]) AS pending_count,
             size([p IN properties WHERE p.verified = true AND p.source = 'notice']) AS verified_count,
             size([p IN properties WHERE p.verified = true AND p.source = 'human']) AS edited_count
        WHERE {notice_filter} AND {search_filter}
        RETURN d.filename                       AS filename,
               d.file_path                      AS file_path,
               d.public_url                     AS public_url,
               d.notice_type                    AS notice_type,
               coalesce(d.property_count, total_count) AS doc_property_count,
               total_count                      AS total_count,
               pending_count                    AS pending_count,
               verified_count                   AS verified_count,
               edited_count                     AS edited_count,
               properties                       AS properties,
               d.markdown                       AS markdown
        ORDER BY pending_count DESC,
                 total_count DESC,
                 filename ASC
        SKIP $skip LIMIT $size
    """
    rows = run_read_query(cypher, params, max_rows=size, timeout=30.0)

    count_cypher = f"""
        MATCH (d:Document)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
        WHERE a.description_source IN ['notice', 'human']
          AND a.description IS NOT NULL
          {date_predicate}
        OPTIONAL MATCH (a)-[:HAS_BORROWER]->(b:Borrower)
        WITH d, a, collect(DISTINCT b.name) AS borrowers
        WITH d, collect({{
                auction_id:    a.auction_id,
                title:         a.title,
                borrowers:     borrowers,
                reserve_price: a.reserve_price_num,
                completeness:  a.description_completeness,
                source:        a.description_source,
                verified:      coalesce(a.description_verified, false)
             }}) AS properties
        WITH d, properties,
             size([p IN properties WHERE p.verified = false]) AS pending_count,
             size([p IN properties WHERE p.verified = true AND p.source = 'human']) AS edited_count
        WHERE {notice_filter} AND {search_filter}
        RETURN count(d) AS total
    """
    count_rows = run_read_query(count_cypher, params, max_rows=1, timeout=30.0)
    total = count_rows[0]["total"] if count_rows else 0

    for r in rows:
        for p in r.get("properties") or []:
            v = p.get("verified_at")
            if v is not None and not isinstance(v, str):
                p["verified_at"] = str(v)
        _sort_properties_by_markdown(r)

    return {"page": page, "size": size, "total": total, "rows": rows}


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
    return rows[0] if rows else None


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


def list_classification_queue(
    status: ClassificationStatus = "pending",
    q: str | None = None,
    page: int = 1,
    size: int = 50,
    confidence_min: float | None = None,
    confidence_max: float | None = None,
    agrees_only: bool = False,
    notice_type: NoticeTypeFilter | None = None,
) -> dict:
    """Return a page of :Document nodes for the classification review queue.

    Status semantics:
      - pending:  not yet human-verified (notice_type_verified_at IS NULL)
      - verified: human confirmed, type unchanged (notice_type_overridden = false)
      - edited:   human overrode the type (notice_type_overridden = true)
      - all:      every Document with a notice_type

    confidence_min / agrees_only are independent filters layered on top of
    status — used by the "auto-confirm" UI to surface unverified notices
    whose classifier prediction already matches notice_type at or above a
    chosen confidence threshold.
    """
    page = max(1, int(page))
    size = max(1, min(200, int(size)))
    skip = (page - 1) * size

    where = ["d.notice_type IS NOT NULL"]
    if status == "pending":
        where.append("d.notice_type_verified_at IS NULL")
    elif status == "verified":
        where.append("d.notice_type_verified_at IS NOT NULL")
        where.append("coalesce(d.notice_type_overridden, false) = false")
    elif status == "edited":
        where.append("d.notice_type_verified_at IS NOT NULL")
        where.append("coalesce(d.notice_type_overridden, false) = true")
    # "all" → no extra filter

    params: dict = {"skip": skip, "size": size}
    if q:
        where.append("toLower(coalesce(d.filename, '')) CONTAINS toLower($q)")
        params["q"] = q
    if confidence_min is not None:
        where.append("coalesce(d.notice_type_confidence, 0.0) >= $confidence_min")
        params["confidence_min"] = float(confidence_min)
    if confidence_max is not None:
        where.append("coalesce(d.notice_type_confidence, 0.0) <= $confidence_max")
        params["confidence_max"] = float(confidence_max)
    if agrees_only:
        where.append("d.notice_type_classifier_pred IS NOT NULL")
        where.append("d.notice_type = d.notice_type_classifier_pred")

    nt_clause = _notice_type_clause(notice_type, alias="d")
    if nt_clause:
        where.append(nt_clause)

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
               d.notice_type_classifier_pred    AS classifier_pred,
               d.notice_type_confidence         AS classifier_confidence,
               d.notice_type_reasoning          AS classifier_reasoning,
               d.notice_type_model              AS classifier_model,
               toString(d.notice_type_classified_at) AS classified_at,
               coalesce(d.notice_type_overridden, false) AS overridden,
               (d.notice_type_verified_at IS NOT NULL) AS verified,
               toString(d.notice_type_verified_at) AS verified_at,
               d.notice_type_verified_by        AS verified_by,
               d.notice_type_review_notes       AS review_notes,
               d.description_extraction_status  AS extraction_status,
               (d.notice_type_classifier_pred IS NOT NULL
                AND d.notice_type <> d.notice_type_classifier_pred) AS disagreement,
               [t IN titles WHERE t IS NOT NULL][0..3] AS sample_titles,
               size(auction_ids)                AS auction_id_count
        ORDER BY disagreement DESC,
                 verified ASC,
                 coalesce(d.notice_type_confidence, 0.0) ASC,
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
    confidence_min: float | None = None,
    confidence_max: float | None = None,
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

    if confidence_min is not None:
        where.append("coalesce(d.notice_type_confidence, 0.0) >= $confidence_min")
        params["confidence_min"] = float(confidence_min)
    if confidence_max is not None:
        where.append("coalesce(d.notice_type_confidence, 0.0) <= $confidence_max")
        params["confidence_max"] = float(confidence_max)

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
               d.notice_type_confidence           AS notice_type_confidence,
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
) -> dict:
    """Counts for the classification queue header."""
    nt_clause = _notice_type_clause(notice_type, alias="d")
    nt_where = f" AND {nt_clause}" if nt_clause else ""
    rows = run_read_query(f"""
        MATCH (d:Document)
        WHERE d.notice_type IS NOT NULL{nt_where}
        RETURN
          count(*) AS total,
          sum(CASE WHEN d.notice_type_verified_at IS NULL THEN 1 ELSE 0 END) AS pending,
          sum(CASE WHEN d.notice_type_verified_at IS NOT NULL
                    AND coalesce(d.notice_type_overridden, false) = false
                   THEN 1 ELSE 0 END) AS verified,
          sum(CASE WHEN d.notice_type_verified_at IS NOT NULL
                    AND coalesce(d.notice_type_overridden, false) = true
                   THEN 1 ELSE 0 END) AS edited
    """, max_rows=1)
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
) -> dict | None:
    """Set a Document's notice_type from human review.

    Side effects when the new notice_type differs from the prior:
      - description_extraction_status is set to 'needs_reextract' so the
        next pipeline run regenerates the cache file and apply.
      - Every linked AuctionProperty whose description was NOT human-edited
        is unverified (description_verified=false; audit fields cleared).
        The description text stays in place — it gets overwritten by the
        next pipeline run, after which the reviewer re-verifies.
      - Human-edited rows (description_source='human') are NEVER touched;
        their edits stand regardless of classification flips.

    Returns a result row or None if no Document had that filename.
    """
    if notice_type not in ("single", "multi"):
        raise ValueError("notice_type must be 'single' or 'multi'")
    params = {"filename": filename, "nt": notice_type,
              "by": by_email, "notes": notes}
    rows = run_query("""
        MATCH (d:Document {filename: $filename})
        WITH d, d.notice_type AS prior
        SET d.notice_type                  = $nt,
            d.notice_type_overridden       = true,
            d.notice_type_verified_at      = datetime(),
            d.notice_type_verified_by      = $by,
            d.notice_type_review_notes     = CASE
                WHEN $notes IS NULL OR $notes = ''
                THEN d.notice_type_review_notes
                ELSE $notes END,
            d.description_extraction_status = CASE
                WHEN prior = $nt
                THEN d.description_extraction_status
                ELSE 'needs_reextract' END
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
               toString(d.notice_type_verified_at) AS verified_at,
               d.notice_type_verified_by           AS verified_by,
               d.notice_type_review_notes          AS review_notes,
               d.description_extraction_status     AS extraction_status,
               size(to_invalidate)                 AS invalidated_count
    """, params)
    return rows[0] if rows else None


def auto_confirm_classifications(
    confidence_min: float,
    by_email: str,
    notes: str | None = None,
    dry_run: bool = False,
    confidence_max: float = 1.0,
) -> dict:
    """Bulk-verify Documents where the classifier already agrees with the
    current notice_type AND confidence >= confidence_min AND the Document
    has not been human-verified yet.

    Because the classifier agrees with the existing notice_type, this does
    NOT flip the type, does NOT trigger re-extract, and does NOT set
    notice_type_overridden — there is no override happening; the human is
    rubber-stamping the model's agreement with the cluster-count seed.

    Returns ``{"count": N, "dry_run": bool}``. When ``dry_run=True`` nothing
    is written; the count reflects what *would* be confirmed.
    """
    params = {
        "min_conf": float(confidence_min),
        "max_conf": float(confidence_max),
        "by": by_email,
        "notes": notes,
    }
    if dry_run:
        rows = run_read_query(
            """
            MATCH (d:Document)
            WHERE d.notice_type IS NOT NULL
              AND d.notice_type_verified_at IS NULL
              AND d.notice_type_classifier_pred IS NOT NULL
              AND d.notice_type = d.notice_type_classifier_pred
              AND coalesce(d.notice_type_confidence, 0.0) >= $min_conf
              AND coalesce(d.notice_type_confidence, 0.0) <= $max_conf
            RETURN count(d) AS n
            """,
            params,
            max_rows=1,
        )
        return {"count": int(rows[0]["n"]) if rows else 0, "dry_run": True}

    rows = run_query(
        """
        MATCH (d:Document)
        WHERE d.notice_type IS NOT NULL
          AND d.notice_type_verified_at IS NULL
          AND d.notice_type_classifier_pred IS NOT NULL
          AND d.notice_type = d.notice_type_classifier_pred
          AND coalesce(d.notice_type_confidence, 0.0) >= $min_conf
          AND coalesce(d.notice_type_confidence, 0.0) <= $max_conf
        SET d.notice_type_verified_at  = datetime(),
            d.notice_type_verified_by  = $by,
            d.notice_type_review_notes = CASE
                WHEN $notes IS NULL OR $notes = ''
                THEN d.notice_type_review_notes
                ELSE $notes END
        RETURN count(d) AS n
        """,
        params,
    )
    return {"count": int(rows[0]["n"]) if rows else 0, "dry_run": False}


def stats(
    date_from: str | None = None,
    date_to: str | None = None,
    notice_type: NoticeTypeFilter | None = None,
) -> dict:
    """Counts for the queue header — pending / verified / edited / total.

    Optional date_from / date_to filter properties by auction_start_dt so the
    pills reflect the current date filter the reviewer has applied.

    Optional notice_type filter scopes stats to properties backed by a
    Document of the given type. When filtering, an OPTIONAL MATCH to Document
    is added; properties with no Document have d.notice_type = NULL, so they
    are excluded by single/multi filters and included by unclassified.
    """
    where = ["a.description_source IN ['notice', 'human']", "a.description IS NOT NULL"]
    params: dict = {}
    if date_from:
        where.append("a.auction_start_dt IS NOT NULL AND date(a.auction_start_dt) >= date($date_from)")
        params["date_from"] = date_from
    if date_to:
        where.append("a.auction_start_dt IS NOT NULL AND date(a.auction_start_dt) <= date($date_to)")
        params["date_to"] = date_to

    nt_clause = _notice_type_clause(notice_type, alias="d")
    if nt_clause:
        where.append(nt_clause)

    where_str = ' AND '.join(where)

    if nt_clause:
        # Need to join Document to apply the filter; OPTIONAL MATCH so that
        # properties without a document still participate (NULL d.notice_type
        # satisfies `IS NULL` but fails `= 'single'/'multi'` — correct).
        cypher = f"""
            MATCH (a:AuctionProperty)
            OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(d:Document)
            WITH a, head(collect(d)) AS d
            WHERE {where_str}
            RETURN
              count(*) AS total,
              sum(CASE WHEN coalesce(a.description_verified, false) = false THEN 1 ELSE 0 END) AS pending,
              sum(CASE WHEN coalesce(a.description_verified, false) = true
                        AND a.description_source = 'notice' THEN 1 ELSE 0 END) AS verified,
              sum(CASE WHEN coalesce(a.description_verified, false) = true
                        AND a.description_source = 'human' THEN 1 ELSE 0 END) AS edited
        """
    else:
        cypher = f"""
            MATCH (a:AuctionProperty)
            WHERE {where_str}
            RETURN
              count(*) AS total,
              sum(CASE WHEN coalesce(a.description_verified, false) = false THEN 1 ELSE 0 END) AS pending,
              sum(CASE WHEN coalesce(a.description_verified, false) = true
                        AND a.description_source = 'notice' THEN 1 ELSE 0 END) AS verified,
              sum(CASE WHEN coalesce(a.description_verified, false) = true
                        AND a.description_source = 'human' THEN 1 ELSE 0 END) AS edited
        """
    rows = run_read_query(cypher, params, max_rows=1)
    if not rows:
        return {"total": 0, "pending": 0, "verified": 0, "edited": 0}
    r = rows[0]
    return {
        "total": int(r.get("total") or 0),
        "pending": int(r.get("pending") or 0),
        "verified": int(r.get("verified") or 0),
        "edited": int(r.get("edited") or 0),
    }


# ── Markdown-quality review ─────────────────────────────────────────────────
# A :Document's `markdown` is OCR output. Coverage scoring
# (pipeline/score_markdown.py) writes `markdown_quality_score` 0–100 per
# Document; reviewers focus on the low end. Verify writes:
#   - markdown_quality              ('good' | 'bad')
#   - markdown_verified_at          (datetime)
#   - markdown_verified_by          (admin email)
#   - markdown_review_notes         (optional)


MarkdownStatus = Literal["pending", "verified", "edited", "all"]


def _markdown_where(
    status: MarkdownStatus,
    score_min: float | None,
    score_max: float | None = None,
    notice_type: NoticeTypeFilter | None = None,
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
    if score_min is not None:
        where.append("d.markdown_quality_score IS NOT NULL")
        where.append("d.markdown_quality_score >= $score_min")
        params["score_min"] = float(score_min)
    if score_max is not None:
        where.append("d.markdown_quality_score IS NOT NULL")
        where.append("d.markdown_quality_score <= $score_max")
        params["score_max"] = float(score_max)
    clause = _notice_type_clause(notice_type, alias="d")
    if clause:
        where.append(clause)
    return where, params


def markdown_stats(
    score_min: float = 70.0,
    score_max: float = 100.0,
    notice_type: NoticeTypeFilter | None = None,
) -> dict:
    """Counts for the markdown review header.

    `auto_confirmable` = unverified Documents whose score ≥ score_min and
    ≤ score_max — the pile the bulk-confirm button would clear at the current
    slider values.
    """
    nt_clause = _notice_type_clause(notice_type, alias="d")
    nt_where = f" AND {nt_clause}" if nt_clause else ""
    rows = run_read_query(
        f"""
        MATCH (d:Document)
        WHERE d.markdown IS NOT NULL AND d.markdown <> ''{nt_where}
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
        {"score_min": float(score_min), "score_max": float(score_max)},
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


def list_markdown_queue(
    status: MarkdownStatus = "pending",
    q: str | None = None,
    page: int = 1,
    size: int = 50,
    score_min: float | None = None,
    score_max: float | None = None,
    notice_type: NoticeTypeFilter | None = None,
) -> dict:
    """Return a page of Documents for markdown-quality review.

    Order: pending first, then lowest score first (so the worst OCR floats
    to the top of the reviewer's queue). `score_min` lets the UI restrict
    the queue to Documents at or above the auto-confirm threshold — what
    the bulk-confirm button is about to clear.
    """
    page = max(1, int(page))
    size = max(1, min(200, int(size)))
    skip = (page - 1) * size

    where, params = _markdown_where(status, score_min, score_max, notice_type)
    params.update({"skip": skip, "size": size})
    if q:
        where.append("toLower(coalesce(d.filename, '')) CONTAINS toLower($q)")
        params["q"] = q
    where_clause = " AND ".join(where)

    cypher = f"""
        MATCH (d:Document)
        WHERE {where_clause}
        OPTIONAL MATCH (d)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
        WITH d, count(DISTINCT a) AS prop_count
        RETURN d.filename                       AS filename,
               d.file_path                      AS file_path,
               d.public_url                     AS public_url,
               d.notice_type                    AS notice_type,
               coalesce(d.property_count, prop_count) AS property_count,
               size(d.markdown)                 AS markdown_length,
               d.markdown                       AS markdown,
               d.markdown_quality_score         AS score,
               d.markdown_quality               AS quality,
               (d.markdown_verified_at IS NOT NULL) AS verified,
               toString(d.markdown_verified_at) AS verified_at,
               d.markdown_verified_by           AS verified_by,
               d.markdown_review_notes          AS review_notes,
               toString(d.markdown_reextracted_at) AS reextracted_at,
               d.markdown_reextracted_by        AS reextracted_by
        ORDER BY (d.markdown_verified_at IS NULL) DESC,
                 coalesce(d.markdown_quality_score, -1.0) ASC,
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

    if score_min is not None:
        where.append("d.markdown_quality_score IS NOT NULL")
        where.append("d.markdown_quality_score >= $score_min")
        params["score_min"] = float(score_min)
    if score_max is not None:
        where.append("d.markdown_quality_score IS NOT NULL")
        where.append("d.markdown_quality_score <= $score_max")
        params["score_max"] = float(score_max)

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
               d.markdown_quality                 AS quality,
               (d.markdown_verified_at IS NOT NULL) AS verified,
               toString(d.markdown_verified_at)   AS verified_at
        ORDER BY verified ASC,
                 coalesce(d.markdown_quality_score, -1.0) ASC,
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
               d.markdown_quality_score         AS score,
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
) -> dict:
    """Bulk-verify (quality='good') every unverified Document with
    score_min ≤ score ≤ score_max. Returns ``{"count": N, "dry_run": bool}``.
    """
    params = {
        "min": float(score_min),
        "max": float(score_max),
        "by": by_email,
        "notes": notes,
    }
    if dry_run:
        rows = run_read_query(
            """
            MATCH (d:Document)
            WHERE d.markdown IS NOT NULL AND d.markdown <> ''
              AND d.markdown_verified_at IS NULL
              AND d.markdown_quality_score IS NOT NULL
              AND d.markdown_quality_score >= $min
              AND d.markdown_quality_score <= $max
            RETURN count(d) AS n
            """,
            params,
            max_rows=1,
        )
        return {"count": int(rows[0]["n"]) if rows else 0, "dry_run": True}

    rows = run_query(
        """
        MATCH (d:Document)
        WHERE d.markdown IS NOT NULL AND d.markdown <> ''
          AND d.markdown_verified_at IS NULL
          AND d.markdown_quality_score IS NOT NULL
          AND d.markdown_quality_score >= $min
          AND d.markdown_quality_score <= $max
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
