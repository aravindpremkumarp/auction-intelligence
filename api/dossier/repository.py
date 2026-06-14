"""
api/dossier/repository.py
-------------------------
Cypher gateway for private per-user document dossiers.

Graph shape (all user-scoped, never merged into the public auction graph):

    (:User {supabase_id})-[:OWNS]->(:Dossier)
    (:Dossier)-[:FOR_PROPERTY]->(:AuctionProperty | :UserProperty)
    (:Dossier)-[:CONTAINS]->(:DossierDocument)

Two deliberate choices, both safety-driven:

* **Label is ``:DossierDocument``, not ``:Document``.** The public pipeline
  already owns ``:Document`` (auction notices) and sweeps it with
  ``MATCH (d:Document) WHERE d.markdown ...`` in classify/describe passes — a
  bare ``:Document`` for user uploads would let those passes scoop up private
  files. A distinct label keeps the private dossier graph genuinely separate.
* **Every read/write is gated by the ownership edge**
  ``(:User {supabase_id:$sub})-[:OWNS]->(d:Dossier {id:$did})``. A request
  carrying another user's ``dossier_id`` simply matches zero rows — so it 404s
  rather than leaking. ``owner_supabase_id`` is also stamped on the node as
  defence-in-depth and for indexed lookups.
"""
from __future__ import annotations

from api.neo4j_client import run_query_async, run_read_query_async


# Fields selected for a property regardless of whether it's on- or off-graph.
# AuctionProperty exposes ``auction_id``/``title``; UserProperty exposes
# ``id``/``label`` plus the free-text survey fields.
def _normalize_property(row: dict) -> dict | None:
    labels = row.get("prop_labels") or []
    if "UserProperty" in labels:
        return {
            "kind": "user_property",
            "id": row.get("user_property_id"),
            "label": row.get("property_label"),
            "survey_no": row.get("survey_no"),
            "sub_registrar": row.get("sub_registrar"),
            "address": row.get("address"),
        }
    if "AuctionProperty" in labels:
        return {
            "kind": "auction_property",
            "auction_id": row.get("auction_id"),
            "label": row.get("property_label"),
        }
    return None


async def create_dossier_for_auction(
    supabase_id: str, dossier_id: str, title: str, auction_id: str
) -> dict | None:
    """Create a dossier linked to an existing scraped auction.

    Returns the new dossier summary, or ``None`` when the auction id doesn't
    exist in the graph (router maps that to 404).
    """
    rows = await run_query_async(
        """
        MATCH (u:User {supabase_id: $sub})
        MATCH (a:AuctionProperty {auction_id: $aid})
        CREATE (d:Dossier {
            id: $did, title: $title, owner_supabase_id: $sub,
            created_at: datetime(), updated_at: datetime()
        })
        CREATE (u)-[:OWNS]->(d)
        CREATE (d)-[:FOR_PROPERTY]->(a)
        RETURN d.id AS id, d.title AS title,
               toString(d.created_at) AS created_at,
               toString(d.updated_at) AS updated_at,
               labels(a) AS prop_labels, a.auction_id AS auction_id,
               a.id AS user_property_id, a.title AS property_label,
               a.survey_no AS survey_no, a.sub_registrar AS sub_registrar,
               a.address AS address
        """,
        {"sub": supabase_id, "did": dossier_id, "title": title, "aid": auction_id},
    )
    return _row_to_dossier(rows[0]) if rows else None


async def create_dossier_for_user_property(
    supabase_id: str,
    dossier_id: str,
    title: str,
    user_property_id: str,
    *,
    label: str,
    survey_no: str | None,
    sub_registrar: str | None,
    address: str | None,
) -> dict:
    """Create an off-graph property the user wants to vet plus its dossier."""
    rows = await run_query_async(
        """
        MATCH (u:User {supabase_id: $sub})
        CREATE (p:UserProperty {
            id: $pid, owner_supabase_id: $sub, label: $label,
            survey_no: $survey_no, sub_registrar: $sub_reg, address: $address,
            created_at: datetime()
        })
        CREATE (d:Dossier {
            id: $did, title: $title, owner_supabase_id: $sub,
            created_at: datetime(), updated_at: datetime()
        })
        CREATE (u)-[:OWNS]->(d)
        CREATE (d)-[:FOR_PROPERTY]->(p)
        RETURN d.id AS id, d.title AS title,
               toString(d.created_at) AS created_at,
               toString(d.updated_at) AS updated_at,
               labels(p) AS prop_labels, p.auction_id AS auction_id,
               p.id AS user_property_id, p.label AS property_label,
               p.survey_no AS survey_no, p.sub_registrar AS sub_registrar,
               p.address AS address
        """,
        {
            "sub": supabase_id, "did": dossier_id, "title": title,
            "pid": user_property_id, "label": label, "survey_no": survey_no,
            "sub_reg": sub_registrar, "address": address,
        },
    )
    return _row_to_dossier(rows[0])


async def list_dossiers(supabase_id: str) -> list[dict]:
    """Every dossier the user owns, newest first, with doc counts + the set of
    present doc types (so the router can compute each readiness score)."""
    rows = await run_read_query_async(
        """
        MATCH (u:User {supabase_id: $sub})-[:OWNS]->(d:Dossier)
        OPTIONAL MATCH (d)-[:FOR_PROPERTY]->(prop)
        OPTIONAL MATCH (d)-[:CONTAINS]->(doc:DossierDocument)
        WITH d, prop,
             [x IN collect(DISTINCT doc.doc_type) WHERE x IS NOT NULL] AS doc_types,
             count(DISTINCT doc) AS doc_count
        RETURN d.id AS id, d.title AS title,
               toString(d.created_at) AS created_at,
               toString(d.updated_at) AS updated_at,
               labels(prop) AS prop_labels, prop.auction_id AS auction_id,
               prop.id AS user_property_id,
               coalesce(prop.label, prop.title) AS property_label,
               prop.survey_no AS survey_no, prop.sub_registrar AS sub_registrar,
               prop.address AS address,
               doc_types, doc_count
        ORDER BY d.updated_at DESC
        """,
        {"sub": supabase_id},
        max_rows=500,
    )
    out: list[dict] = []
    for r in rows:
        summary = _row_to_dossier(r)
        summary["doc_count"] = int(r.get("doc_count") or 0)
        summary["doc_types"] = list(r.get("doc_types") or [])
        out.append(summary)
    return out


async def get_dossier(supabase_id: str, dossier_id: str) -> dict | None:
    """Full owner-scoped view: property + the contained documents. ``None`` when
    the dossier doesn't exist or isn't owned by this user (router -> 404)."""
    rows = await run_read_query_async(
        """
        MATCH (u:User {supabase_id: $sub})-[:OWNS]->(d:Dossier {id: $did})
        OPTIONAL MATCH (d)-[:FOR_PROPERTY]->(prop)
        OPTIONAL MATCH (d)-[:CONTAINS]->(doc:DossierDocument)
        WITH d, prop, doc ORDER BY doc.uploaded_at ASC
        WITH d, prop, collect(
            CASE WHEN doc IS NULL THEN NULL ELSE {
                id: doc.id, filename: doc.filename, doc_type: doc.doc_type,
                category: doc.category, status: doc.status,
                doc_type_confidence: doc.doc_type_confidence,
                uploaded_at: toString(doc.uploaded_at)
            } END
        ) AS docs_raw
        RETURN d.id AS id, d.title AS title,
               toString(d.created_at) AS created_at,
               toString(d.updated_at) AS updated_at,
               labels(prop) AS prop_labels, prop.auction_id AS auction_id,
               prop.id AS user_property_id,
               coalesce(prop.label, prop.title) AS property_label,
               prop.survey_no AS survey_no, prop.sub_registrar AS sub_registrar,
               prop.address AS address,
               docs_raw
        """,
        {"sub": supabase_id, "did": dossier_id},
        max_rows=1,
    )
    if not rows:
        return None
    r = rows[0]
    dossier = _row_to_dossier(r)
    dossier["documents"] = [d for d in (r.get("docs_raw") or []) if d]
    return dossier


async def get_dossier_r2_keys(supabase_id: str, dossier_id: str) -> list[str] | None:
    """R2 object keys for every document in an owned dossier, for cascade
    deletion. ``None`` when the dossier isn't found/owned (-> 404 before any
    destructive work happens)."""
    rows = await run_read_query_async(
        """
        MATCH (u:User {supabase_id: $sub})-[:OWNS]->(d:Dossier {id: $did})
        OPTIONAL MATCH (d)-[:CONTAINS]->(doc:DossierDocument)
        RETURN [k IN collect(doc.r2_key) WHERE k IS NOT NULL] AS keys
        """,
        {"sub": supabase_id, "did": dossier_id},
        max_rows=1,
    )
    if not rows:
        return None
    return list(rows[0].get("keys") or [])


async def delete_dossier(supabase_id: str, dossier_id: str) -> bool:
    """Cascade-delete an owned dossier: its DossierDocument nodes and the
    off-graph UserProperty it created (never an AuctionProperty — that's public
    graph). Returns True if a dossier was deleted.

    The UserProperty is created 1:1 with its dossier (see
    ``create_dossier_for_user_property``), so deleting it here can't orphan
    another dossier.
    """
    rows = await run_query_async(
        """
        MATCH (u:User {supabase_id: $sub})-[:OWNS]->(d:Dossier {id: $did})
        OPTIONAL MATCH (d)-[:CONTAINS]->(doc:DossierDocument)
        OPTIONAL MATCH (d)-[:FOR_PROPERTY]->(up:UserProperty)
        WITH d, collect(DISTINCT doc) AS docs, collect(DISTINCT up) AS ups
        FOREACH (x IN docs | DETACH DELETE x)
        FOREACH (x IN ups | DETACH DELETE x)
        DETACH DELETE d
        RETURN count(*) AS n
        """,
        {"sub": supabase_id, "did": dossier_id},
    )
    return bool(rows) and int(rows[0].get("n") or 0) > 0


# ── documents ─────────────────────────────────────────────────────────────────
#
# Every document operation is gated through the owning dossier:
#   (:User {supabase_id})-[:OWNS]->(:Dossier {id})-[:CONTAINS]->(:DossierDocument {id})
# so a forged dossier_id/doc_id pairing matches zero rows (router -> 404).

async def owns_dossier(supabase_id: str, dossier_id: str) -> bool:
    """Cheap ownership probe used before accepting an upload (so we don't push
    bytes to R2 for a dossier the caller doesn't own)."""
    rows = await run_read_query_async(
        """
        MATCH (:User {supabase_id: $sub})-[:OWNS]->(d:Dossier {id: $did})
        RETURN d.id AS id
        """,
        {"sub": supabase_id, "did": dossier_id},
        max_rows=1,
    )
    return bool(rows)


async def add_document(
    supabase_id: str, dossier_id: str, doc_id: str, *,
    filename: str, r2_key: str, content_type: str, size_bytes: int,
    status: str, ocr_consent_at: str,
) -> bool:
    """Create a DossierDocument under an owned dossier (status 'processing').
    Returns False if the dossier isn't owned/found."""
    rows = await run_query_async(
        """
        MATCH (u:User {supabase_id: $sub})-[:OWNS]->(d:Dossier {id: $did})
        CREATE (d)-[:CONTAINS]->(doc:DossierDocument {
            id: $doc_id, filename: $filename, r2_key: $r2_key,
            content_type: $content_type, size_bytes: $size_bytes,
            status: $status, ocr_consent_at: datetime($consent_at),
            uploaded_at: datetime()
        })
        SET d.updated_at = datetime()
        RETURN doc.id AS id
        """,
        {
            "sub": supabase_id, "did": dossier_id, "doc_id": doc_id,
            "filename": filename, "r2_key": r2_key, "content_type": content_type,
            "size_bytes": size_bytes, "status": status,
            "consent_at": ocr_consent_at,
        },
    )
    return bool(rows)


async def set_document_result(
    supabase_id: str, dossier_id: str, doc_id: str, *,
    status: str, category: str | None, doc_type: str | None,
    confidence: float | None, reasoning: str | None,
    ocr_text: str | None, classified_at: str | None,
) -> dict | None:
    """Persist the OCR + classification outcome onto a document."""
    rows = await run_query_async(
        """
        MATCH (u:User {supabase_id: $sub})-[:OWNS]->(d:Dossier {id: $did})
              -[:CONTAINS]->(doc:DossierDocument {id: $doc_id})
        SET doc.status = $status,
            doc.category = $category,
            doc.doc_type = $doc_type,
            doc.doc_type_confidence = $confidence,
            doc.doc_type_reasoning = $reasoning,
            doc.ocr_text = $ocr_text,
            doc.classified_at = CASE WHEN $classified_at IS NULL
                                THEN null ELSE datetime($classified_at) END,
            d.updated_at = datetime()
        RETURN doc.id AS id, doc.filename AS filename, doc.doc_type AS doc_type,
               doc.category AS category, doc.status AS status,
               doc.doc_type_confidence AS doc_type_confidence,
               toString(doc.uploaded_at) AS uploaded_at
        """,
        {
            "sub": supabase_id, "did": dossier_id, "doc_id": doc_id,
            "status": status, "category": category, "doc_type": doc_type,
            "confidence": confidence, "reasoning": reasoning,
            "ocr_text": ocr_text, "classified_at": classified_at,
        },
    )
    return rows[0] if rows else None


async def get_document(supabase_id: str, dossier_id: str, doc_id: str) -> dict | None:
    """Fetch one owned document, including its private ``r2_key`` (so the router
    can mint a presigned URL only after the ownership match)."""
    rows = await run_read_query_async(
        """
        MATCH (u:User {supabase_id: $sub})-[:OWNS]->(:Dossier {id: $did})
              -[:CONTAINS]->(doc:DossierDocument {id: $doc_id})
        RETURN doc.id AS id, doc.filename AS filename, doc.r2_key AS r2_key,
               doc.content_type AS content_type, doc.status AS status,
               doc.doc_type AS doc_type, doc.category AS category,
               doc.doc_type_confidence AS doc_type_confidence,
               toString(doc.uploaded_at) AS uploaded_at
        """,
        {"sub": supabase_id, "did": dossier_id, "doc_id": doc_id},
        max_rows=1,
    )
    return rows[0] if rows else None


async def search_user_documents(
    supabase_id: str, *, dossier_id: str | None = None, doc_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Owned, successfully-OCR'd documents with their text, for dossier Q&A.

    Scoped to the caller via OWNS->CONTAINS; optionally narrowed to one dossier
    and/or one doc type. Only ``status='ready'`` documents (those with text) are
    returned. Newest first.
    """
    rows = await run_read_query_async(
        """
        MATCH (u:User {supabase_id: $sub})-[:OWNS]->(d:Dossier)
              -[:CONTAINS]->(doc:DossierDocument)
        WHERE ($did IS NULL OR d.id = $did)
          AND ($doc_type IS NULL OR doc.doc_type = $doc_type)
          AND doc.status = 'ready'
          AND doc.ocr_text IS NOT NULL AND doc.ocr_text <> ''
        OPTIONAL MATCH (d)-[:FOR_PROPERTY]->(prop)
        RETURN d.id AS dossier_id, d.title AS dossier_title,
               coalesce(prop.label, prop.title) AS property_label,
               doc.id AS doc_id, doc.filename AS filename,
               doc.doc_type AS doc_type, doc.category AS category,
               doc.ocr_text AS ocr_text
        ORDER BY doc.uploaded_at DESC
        """,
        {"sub": supabase_id, "did": dossier_id, "doc_type": doc_type},
        max_rows=limit,
    )
    return rows


async def delete_document(supabase_id: str, dossier_id: str, doc_id: str) -> str | None:
    """Delete one owned document, returning its ``r2_key`` for object cleanup.
    Returns ``None`` (sentinel ``""`` is impossible since keys are non-empty)
    when the document isn't found/owned — router maps that to 404."""
    rows = await run_query_async(
        """
        MATCH (u:User {supabase_id: $sub})-[:OWNS]->(d:Dossier {id: $did})
              -[:CONTAINS]->(doc:DossierDocument {id: $doc_id})
        WITH d, doc, doc.r2_key AS key
        DETACH DELETE doc
        SET d.updated_at = datetime()
        RETURN key
        """,
        {"sub": supabase_id, "did": dossier_id, "doc_id": doc_id},
    )
    if not rows:
        return None
    return rows[0].get("key") or ""


# ── shaping ──────────────────────────────────────────────────────────────────

def _row_to_dossier(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "title": row.get("title"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "property": _normalize_property(row),
    }
