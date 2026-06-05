"""
api/health/router.py
--------------------
`/health` (liveness) + `/health/deep` (readiness/diagnostics). Extracted from
api/main.py so the monitoring surface lives next to the data-freshness checks
it shares with `/stats`.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from api.neo4j_client import run_query

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/health/deep")
def health_deep() -> dict:
    """Extended health check: verifies Neo4j connectivity, counts the main
    node label, confirms the vector index exists, and reports how fresh the
    enrichment pipeline's output is. Used by uptime monitoring and during PR
    reviews to catch environment drift or a stalled ingestion job."""
    checks: dict[str, Any] = {"status": "ok", "errors": []}
    try:
        rows = run_query("MATCH (a:AuctionProperty) RETURN count(a) AS n")
        checks["auction_count"] = rows[0]["n"] if rows else 0
    except Exception as e:
        checks["errors"].append(f"neo4j: {e!r}")
    try:
        idx = run_query(
            "SHOW INDEXES YIELD name, type WHERE name = 'property_desc_idx' "
            "RETURN name, type"
        )
        checks["vector_index"] = idx[0] if idx else None
    except Exception as e:
        checks["errors"].append(f"vector_index: {e!r}")
    try:
        # `verified_at` is stamped by pipeline/load_enriched.py on every
        # enrichment upsert, so its max is the freshest the dataset has been.
        rows = run_query(
            "MATCH (a:AuctionProperty) WHERE a.verified_at IS NOT NULL "
            "RETURN toString(max(a.verified_at)) AS last_enriched"
        )
        checks["last_enriched"] = rows[0]["last_enriched"] if rows else None
    except Exception as e:
        checks["errors"].append(f"freshness: {e!r}")
    if checks["errors"]:
        checks["status"] = "degraded"
    return checks
