"""
api/alerts/__init__.py
----------------------
In-app property deadline alerts. Surfaces an alert for each of a user's
saved (`:SAVED`) properties whose auction application deadline falls inside
the 7 / 3 / 1-day threshold windows — computed on-read from
`application_deadline_dt` already in the graph, so no scraper or scheduler is
involved.

This is the minimal, no-new-storage wedge from
docs/design/2026-06-13-property-deadline-alerts.md (Approach A). Persisted
notifications (read/unread, history) and price/status alerts are deferred to
a later phase.
"""
from __future__ import annotations

from api.alerts.router import router

__all__ = ["router"]
