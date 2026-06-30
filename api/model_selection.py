"""
api/model_selection.py
----------------------
Pure (network-free, agent-free) logic for the user-selectable chat model and
reasoning-effort toggles. Kept separate from `api/agent.py` so the gating rules
and the OpenRouter `extra_body` shape can be unit-tested without building the
real pydantic-ai agent (which pulls in Neo4j, tools, etc.), and so the chat
router can import the resolvers without importing the agent.

Two user-facing toggles ride on a /chat request:

  * **model** — "flash" (cheap/fast) or "pro" (deeper reasoning). Free and
    anonymous users are hard-locked to Flash server-side; the client toggle is
    advisory only and is never trusted for the entitlement decision (same
    philosophy as the durable chat-quota gate). Paid users may choose either.
  * **reasoning_effort** — how hard the model thinks. Reasoning tokens bill as
    output, so this is the single biggest per-turn cost lever. `None` means
    "use the server default" (OPENROUTER_CHAT_REASONING_EFFORT).

`build_model_settings` turns a resolved effort into the OpenRouter `extra_body`
dict; `api/agent.py` pairs it with a concrete model object per request.
"""
from __future__ import annotations

from pipeline.config import (
    FREE_TIER_REASONING_EFFORT,
    OPENROUTER_CHAT_PROVIDER_ALLOW_FALLBACKS,
    OPENROUTER_CHAT_PROVIDER_MAX_PRICE,
    OPENROUTER_CHAT_PROVIDER_ORDER,
    OPENROUTER_CHAT_REASONING_EFFORT,
    OPENROUTER_MODEL_CHAT,
    OPENROUTER_MODEL_CHAT_FLASH,
)

# ── Models ───────────────────────────────────────────────────────────────────
# Logical name → OpenRouter slug. The logical names are the stable contract the
# request body and UI toggle speak; the slugs are config-tunable per env.
CHAT_MODEL_SLUGS: dict[str, str] = {
    "flash": OPENROUTER_MODEL_CHAT_FLASH,
    "pro": OPENROUTER_MODEL_CHAT,
}
ALLOWED_MODELS = frozenset(CHAT_MODEL_SLUGS)

# Free/anon users get Flash only; paid users default to Pro but may pick Flash.
FREE_TIER_MODEL = "flash"
DEFAULT_PAID_MODEL = "pro"

# UI option list (consumed by GET /chat/models). `min_tier` lets the client
# render Pro as locked for free users; the server still enforces (below).
CHAT_MODEL_OPTIONS: list[dict] = [
    {
        "id": "flash",
        "label": "Flash",
        "description": "Fast and low-cost. Great for quick lookups and most questions.",
        "min_tier": "free",
    },
    {
        "id": "pro",
        "label": "Pro",
        "description": "Deeper reasoning for complex research and comparisons.",
        "min_tier": "paid",
    },
]

# ── Reasoning effort ──────────────────────────────────────────────────────────
# Values OpenRouter / DeepSeek accept on the `reasoning.effort` param. "off"
# (and synonyms) disables reasoning entirely. We validate against this set and
# fall back to the server default on anything unknown, so a stale client can't
# wedge a turn with a bogus value.
_EFFORT_OFF = {"off", "none", "disabled", "0", "false"}
ALLOWED_REASONING_EFFORTS = frozenset({"off", "none", "low", "medium", "high", "xhigh"})

# UI option list (consumed by GET /chat/models). Kept to a friendly three; the
# request body still accepts any value in ALLOWED_REASONING_EFFORTS.
REASONING_EFFORT_OPTIONS: list[dict] = [
    {"id": "off", "label": "Off", "description": "Fastest and cheapest — no extra reasoning."},
    {"id": "medium", "label": "Balanced", "description": "Moderate reasoning for everyday questions."},
    {"id": "high", "label": "Thorough", "description": "Maximum reasoning for hard, multi-step questions."},
]


def resolve_chat_model(tier: str | None, requested: str | None) -> str:
    """Resolve the logical model name to actually use for this turn.

    Free/anonymous callers (anything that isn't the "paid" tier) are forced
    onto Flash regardless of what the client asked for — this is the
    entitlement gate, enforced server-side. Paid callers get their requested
    model when it's a known one, else the paid default (Pro).
    """
    if tier != "paid":
        return FREE_TIER_MODEL
    req = (requested or "").strip().lower()
    if req in ALLOWED_MODELS:
        return req
    return DEFAULT_PAID_MODEL


# Server-enforced reasoning cap for free/anon. Validated here so a bogus env
# value can't wedge every free turn; falls back to "low" if misconfigured.
FREE_TIER_EFFORT = (
    FREE_TIER_REASONING_EFFORT.strip().lower()
    if FREE_TIER_REASONING_EFFORT.strip().lower() in ALLOWED_REASONING_EFFORTS
    else "low"
)


def resolve_reasoning_effort(requested: str | None, tier: str | None = None) -> str | None:
    """Normalise the requested effort, gated by tier.

    Free/anonymous callers (anything that isn't "paid") are clamped to
    FREE_TIER_EFFORT server-side, regardless of what the client asked for —
    reasoning tokens bill as output, so this is the entitlement gate that keeps
    the free tier cheap (mirrors `resolve_chat_model`). Paid callers get their
    requested effort when valid; `None`/unknown means "use the server default"
    (resolved in `build_model_settings`)."""
    if tier != "paid":
        return FREE_TIER_EFFORT
    if requested is None:
        return None
    eff = requested.strip().lower()
    return eff if eff in ALLOWED_REASONING_EFFORTS else None


def _provider_routing() -> dict | None:
    """Build OpenRouter's `provider` routing block from config, or None when
    no provider pin is configured. Pins chat to first-party DeepSeek so the
    automatic prompt cache lands and input cost stays low (see pipeline/config).
    """
    order = [p.strip() for p in (OPENROUTER_CHAT_PROVIDER_ORDER or "").split(",") if p.strip()]
    if not order:
        return None
    routing: dict = {
        "order": order,
        "allow_fallbacks": OPENROUTER_CHAT_PROVIDER_ALLOW_FALLBACKS.strip().lower()
        in {"1", "true", "yes", "on"},
    }
    max_price = [
        p.strip() for p in (OPENROUTER_CHAT_PROVIDER_MAX_PRICE or "").split(",") if p.strip()
    ]
    if len(max_price) == 2:
        try:
            routing["max_price"] = {
                "prompt": float(max_price[0]),
                "completion": float(max_price[1]),
            }
        except ValueError:
            pass
    return routing


def build_model_settings(reasoning_effort: str | None = None) -> dict:
    """Build the pydantic-ai `model_settings` dict for a chat turn.

    `reasoning_effort=None` uses the server default; pass a resolved value
    (from `resolve_reasoning_effort`) to honor a per-request toggle. The
    `usage.include` flag keeps OpenRouter's detailed token/cache accounting on
    so the per-turn obs log can report cost.
    """
    body: dict = {"usage": {"include": True}}
    routing = _provider_routing()
    if routing:
        body["provider"] = routing
    effort = (
        reasoning_effort if reasoning_effort is not None else OPENROUTER_CHAT_REASONING_EFFORT
    )
    effort = (effort or "").strip().lower()
    if effort and effort not in _EFFORT_OFF:
        body["reasoning"] = {"effort": effort}
    return {"extra_body": body}
