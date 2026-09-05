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
    node label, confirms the two fulltext indexes `semantic_search` needs,
    and reports how fresh the enrichment pipeline's output is. Used by uptime
    monitoring and during PR reviews to catch environment drift or a stalled
    ingestion job."""
    checks: dict[str, Any] = {"status": "ok", "errors": []}
    try:
        rows = run_query("MATCH (a:AuctionProperty) RETURN count(a) AS n")
        checks["auction_count"] = rows[0]["n"] if rows else 0
    except Exception as e:
        checks["errors"].append(f"neo4j: {e!r}")
    try:
        # Both fulltext indexes are load-bearing since the vector lenses were
        # retired (docs/design/2026-08-22-retire-embeddings.md): there is no
        # second engine to degrade to, so a missing index means
        # `semantic_search` returns nothing rather than returning less. Report
        # it as an error, not just a null field.
        rows = run_query(
            "SHOW INDEXES YIELD name, type "
            "WHERE name IN ['lot_description_ft', 'property_text_idx'] "
            "RETURN name, type"
        )
        found = {r["name"] for r in rows}
        checks["fulltext_indexes"] = sorted(found)
        missing = sorted({"lot_description_ft", "property_text_idx"} - found)
        if missing:
            checks["errors"].append(
                f"fulltext_indexes: missing {missing} — semantic_search is degraded"
            )
    except Exception as e:
        checks["errors"].append(f"fulltext_indexes: {e!r}")
    try:
        # `grounded_applied_at` is stamped by pipeline/apply_extractions.py on
        # every grounded write, so its max is the freshest the dataset has
        # been. `verified_at` is the retired legacy enrichment path's stamp,
        # kept as a fallback for rows the grounded path has not reached yet.
        rows = run_query(
            "MATCH (a:AuctionProperty) "
            "WITH coalesce(a.grounded_applied_at, a.verified_at) AS t "
            "WHERE t IS NOT NULL "
            "RETURN toString(max(t)) AS last_enriched"
        )
        checks["last_enriched"] = rows[0]["last_enriched"] if rows else None
    except Exception as e:
        checks["errors"].append(f"freshness: {e!r}")
    if checks["errors"]:
        checks["status"] = "degraded"
    return checks
