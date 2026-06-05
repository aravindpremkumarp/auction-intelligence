"""
api/properties
--------------
Browse-all-properties listing + faceting, single-auction detail, and a
lightweight `/stats` data-freshness snapshot. The Cypher filter/facet
builders are pure functions so they can be unit-tested without a live graph.
"""
from __future__ import annotations

from api.properties.router import router

__all__ = ["router"]
