"""
api/review/queries.py
---------------------
Cypher gateway for the enrichment review queue.
"""
from __future__ import annotations

from typing import Literal

from api.neo4j_client import run_query, run_read_query


ReviewStatus = Literal["pending", "verified", "edited", "all"]


def list_queue(
    status: ReviewStatus = "pending",
    q: str | None = None,
    page: int = 1,
    size: int = 50,
) -> dict:
    """Return a page of properties whose description came from a notice
    extraction (or a human edit), filtered by review status.

    Pending = description_source IN ['notice','human'] AND NOT description_verified.
    Verified = description_verified = true AND description_source = 'notice'.
    Edited = description_verified = true AND description_source = 'human'.
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

    # Total count — strip predicates that reference the document since the
    # count query doesn't pull docs.
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


def stats() -> dict:
    """Counts for the queue header — pending / verified / edited / total."""
    rows = run_read_query(
        """
        MATCH (a:AuctionProperty)
        WHERE a.description_source IN ['notice', 'human'] AND a.description IS NOT NULL
        RETURN
          count(*) AS total,
          sum(CASE WHEN coalesce(a.description_verified, false) = false THEN 1 ELSE 0 END) AS pending,
          sum(CASE WHEN coalesce(a.description_verified, false) = true
                    AND a.description_source = 'notice' THEN 1 ELSE 0 END) AS verified,
          sum(CASE WHEN coalesce(a.description_verified, false) = true
                    AND a.description_source = 'human' THEN 1 ELSE 0 END) AS edited
        """,
        max_rows=1,
    )
    if not rows:
        return {"total": 0, "pending": 0, "verified": 0, "edited": 0}
    r = rows[0]
    return {
        "total": int(r.get("total") or 0),
        "pending": int(r.get("pending") or 0),
        "verified": int(r.get("verified") or 0),
        "edited": int(r.get("edited") or 0),
    }
