"""
api/chat/router.py
------------------
The read-only chat helpers the web UI calls: `/modes` (the mode registry),
`/suggestions` (live starter chips) and `/chat/models` (the tier-aware model
and reasoning-effort toggles).

The pydantic-ai chat itself (`POST /chat`, `POST /chat/stream`) is retired:
every chat turn now runs on `api/agent3/` (`/chat/agent3`). The SSE helpers
the streaming endpoints share moved to `api/chat/sse.py`.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends

from api.auth import get_optional_user
from api.auth.schemas import UserOut
from api.chat.suggestions import build_suggestions
from api.model_selection import (
    CHAT_MODEL_OPTIONS,
    DEFAULT_PAID_MODEL,
    EFFORT_RANK,
    FREE_TIER_EFFORT,
    FREE_TIER_MODEL,
    REASONING_EFFORT_OPTIONS,
)
from api.tools import cypher_tools as cypher_T

logger = logging.getLogger(__name__)

router = APIRouter()


# The modes the UI's picker offers. agent3 takes no mode parameter: a deep
# pass on one property is its `diligence` skill (api/agent3/skills.py).
# `compare` and `report` are parked in modes/_archive/ (2026-07).
# Example ids use the REAL format (plain 6-digit strings like "750879") —
# the old "AUC-12345" chips taught users an id shape that can't exist, so
# copying them seeded guaranteed not-found lookups.
_AVAILABLE_MODES: list[dict[str, Any]] = [
    {
        "id": "ask",
        "label": "Ask",
        "description": "Free-form Q&A over the Neo4j graph (default).",
        "examples": [
            "Residential auctions in Chennai under 30 lakhs",
            "What is the price range in Kanchipuram?",
            "Show auctions with deadline in the next 7 days",
            "How many banks have auctions in Chennai?",
            "Which borrowers have more than 3 properties?",
            "List all cities in the database",
        ],
    },
    {
        "id": "deep-research",
        "label": "Deep research",
        "description": "Full due-diligence workflow on one auction_id.",
        "examples": [
            "Deep research on auction 750879",
        ],
    },
]


@router.get("/modes")
def list_modes() -> dict:
    """Mode registry consumed by the web UI to render the mode selector and
    suggestion chips. Mirrors the career-ops pattern of surfacing each
    markdown mode file as a user-facing entry point."""
    return {"modes": _AVAILABLE_MODES}


# Live starter chips for the chat landing (see api/chat/suggestions.py). Built
# from single-dimension `search_auctions(group_by=...)` distributions — which
# are future-only by default, so a chip's count matches what a click returns
# and no chip can dead-end. Cached in-process for _SUGGESTIONS_TTL_SECONDS: the
# buckets only move when the loader ingests/expires auctions, so an hourly
# refresh is plenty (same rationale as graph_property_count_async's cache) and
# keeps the landing off the graph on every page load.
_SUGGESTIONS_CACHE: dict[str, tuple[float, list[dict]]] = {}
_SUGGESTIONS_TTL_SECONDS = 3600.0
# One distribution query each; order here doesn't matter (the pure builder's
# _PICK_ORDER decides the chip mix), but keep it to dimensions the builder
# knows how to phrase.
_SUGGESTION_DIMS = ("city", "property_type", "asset_category", "area")


def _load_suggestion_distributions() -> dict[str, list[dict]]:
    """Pull each dimension's live distribution. A single dimension's failure
    degrades to fewer chips rather than none; a full outage returns {} and the
    endpoint serves the last good set (or lets the UI keep its fallback)."""
    out: dict[str, list[dict]] = {}
    for dim in _SUGGESTION_DIMS:
        try:
            res = cypher_T.search_auctions(group_by=dim)
        except Exception:  # noqa: BLE001 - one bad dim must not sink the rest
            logger.exception("suggestions: distribution query failed for %r", dim)
            continue
        dist = res.get("distribution") if isinstance(res, dict) else None
        if isinstance(dist, list) and dist:
            out[dim] = dist
    return out


@router.get("/suggestions")
def list_suggestions() -> dict:
    """Data-driven starter chips for the chat landing, cached hourly. Sync (runs
    in FastAPI's threadpool) because the underlying graph reads are blocking.
    Best-effort throughout: on a cold cache + DB hiccup it returns an empty list
    and the web UI keeps its hardcoded fallback chips."""
    now = time.time()
    cached = _SUGGESTIONS_CACHE.get("default")
    if cached and (now - cached[0]) < _SUGGESTIONS_TTL_SECONDS:
        return {"suggestions": cached[1]}
    chips = build_suggestions(_load_suggestion_distributions())
    if chips:
        _SUGGESTIONS_CACHE["default"] = (now, chips)
    elif cached:
        # Refresh hit an empty/failed read — serve the last good set rather
        # than blanking the landing.
        return {"suggestions": cached[1]}
    return {"suggestions": chips}


@router.get("/chat/models")
def list_chat_models(user: UserOut | None = Depends(get_optional_user)) -> dict:
    """Model + reasoning-effort options for the chat toggles, tier-aware.

    Each model carries `locked`: true when the caller's tier can't use it
    (free/anon → Pro is locked). The UI should render locked models disabled
    with an upgrade prompt; the server enforces the same rule on every chat
    turn regardless (`api/chat/gating.py::resolve_turn_model`), so this is purely to drive the toggle UI. `defaults` is what
    the server will use when the client sends no explicit choice.
    """
    tier = user.tier if user else "free"
    is_paid = tier == "paid"
    models = [
        {**opt, "locked": opt["min_tier"] == "paid" and not is_paid}
        for opt in CHAT_MODEL_OPTIONS
    ]
    # Efforts above the free-tier ceiling are clamped server-side, so render
    # them locked for free/anon — picking them would silently do nothing.
    # At/below the ceiling (notably Off) stays available to everyone.
    efforts = [
        {
            **opt,
            "locked": not is_paid
            and EFFORT_RANK[opt["id"]] > EFFORT_RANK[FREE_TIER_EFFORT],
        }
        for opt in REASONING_EFFORT_OPTIONS
    ]
    return {
        "tier": tier,
        "models": models,
        "reasoning_efforts": efforts,
        "defaults": {
            "model": DEFAULT_PAID_MODEL if is_paid else FREE_TIER_MODEL,
            # Free/anon effort is capped server-side; report the real value so
            # the toggle UI doesn't promise reasoning levels they won't get.
            "reasoning_effort": "high" if is_paid else FREE_TIER_EFFORT,
        },
    }
