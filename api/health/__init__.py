"""
api/health
----------
Liveness + deep health checks. `/health` is the cheap load-balancer probe;
`/health/deep` validates Neo4j connectivity, the main node count, the vector
index, and ingestion freshness so monitoring can alert on a stalled pipeline
or a missing index before users notice.
"""
from __future__ import annotations

from api.health.router import router

__all__ = ["router"]
