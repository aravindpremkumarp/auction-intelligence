"""
api/dossier
-----------
Private, per-property document dossier (locker). See
docs/design/2026-06-13-document-dossier-ai-analysis.md.
"""
from __future__ import annotations

from api.dossier.router import router

__all__ = ["router"]
