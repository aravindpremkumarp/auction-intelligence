"""
scripts/resolution_decisions.py
-------------------------------
Read stored human verdicts for the resolvers.

Decisions live as ``(:ResolutionDecision {key, kind, verdict, payload_json,
decided_at, decided_by})`` nodes — one per fact a human settled. The review
API writes them; both resolution scripts call :func:`load_decisions` first and
hand the list to :mod:`pipeline.resolution_review`, which is where the logic
lives. This module is only the graph read.
"""
from __future__ import annotations

import json

from scripts.score_ink_coverage import nq


def load_decisions() -> list[dict]:
    rows = nq("""
        MATCH (r:ResolutionDecision)
        RETURN r.key, r.kind, r.verdict, r.payload_json
    """)
    out: list[dict] = []
    for key, kind, verdict, payload_json in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except (TypeError, ValueError):
            payload = {}
        out.append({"key": key, "kind": kind, "verdict": verdict,
                    "payload": payload})
    return out
