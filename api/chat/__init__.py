"""
api/chat
--------
Chat plumbing shared across the chat endpoints: quota and model gating
(`gating.py`), SSE framing (`sse.py`), panel sync (`panel.py`), and the
read-only `/modes`, `/suggestions`, `/chat/models` router. The admin loops
live in `v2/` and `deep/`; the chat users talk to is `api/agent3/`.
"""
from __future__ import annotations

from api.chat.router import router

__all__ = ["router"]
