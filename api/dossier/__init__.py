"""
api/dossier
-----------
Private, per-property document dossier (locker). See
docs/design/2026-06-13-document-dossier-ai-analysis.md.
"""
from __future__ import annotations

import os

from api.dossier.router import router

__all__ = ["router", "dossiers_enabled"]


def dossiers_enabled() -> bool:
    """Feature flag for the private document dossier.

    Off by default so the feature ships dark for the public release — its API
    routes (main.py) and the agent's `query_user_dossier` tool (agent.py) are
    only wired up when this returns True. Flip ``DOSSIERS_ENABLED=true``
    (or 1/yes/on) on a staging/test deploy to exercise it end-to-end. The
    frontend mirrors this with a ``?dossiers=1`` escape hatch.
    """
    return os.environ.get("DOSSIERS_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
