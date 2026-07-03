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
#
# NB: "low"/"medium" are deliberately NOT in this set. OpenRouter's DeepSeek
# V4 Pro/Flash only implement "high" and "xhigh" (xhigh = max) as distinct
# effort levels — per DeepSeek's own compatibility spec, "low"/"medium"
# requests normalize to "high", so offering them as cheaper options was
# misleading (confirmed empirically: zero "effort" values ever showed up in
# Logfire's chat spans, and OpenRouter's docs for these two models list only
# high/xhigh). "off" is the only genuinely cheaper tier below "high".
_EFFORT_OFF = {"off", "none", "disabled", "0", "false"}
ALLOWED_REASONING_EFFORTS = frozenset({"off", "none", "high", "xhigh"})

# Cost ordering for the free-tier ceiling: a request at or below the cap is
# honored (off is the cheapest — never refuse it), anything above is clamped.
EFFORT_RANK = {"off": 0, "none": 0, "high": 1, "xhigh": 2}

# UI option list (consumed by GET /chat/models). The request body still
# accepts any value in ALLOWED_REASONING_EFFORTS.
REASONING_EFFORT_OPTIONS: list[dict] = [
    {"id": "off", "label": "Off", "description": "Fastest and cheapest — no extra reasoning."},
    {"id": "high", "label": "Thorough", "description": "Deeper reasoning for hard, multi-step questions."},
    {"id": "xhigh", "label": "Maximum", "description": "The most exhaustive reasoning DeepSeek supports."},
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
# value can't wedge every free turn; falls back to "off" if misconfigured —
# the only tier below "high" that's actually honored by DeepSeek V4 (see the
# ALLOWED_REASONING_EFFORTS note above).
FREE_TIER_EFFORT = (
    FREE_TIER_REASONING_EFFORT.strip().lower()
    if FREE_TIER_REASONING_EFFORT.strip().lower() in ALLOWED_REASONING_EFFORTS
    else "off"
)


def resolve_reasoning_effort(requested: str | None, tier: str | None = None) -> str | None:
    """Normalise the requested effort, gated by tier.

    Free/anonymous callers (anything that isn't "paid") get a CEILING of
    FREE_TIER_EFFORT, enforced server-side — reasoning tokens bill as output,
    so this is the entitlement gate that keeps the free tier cheap (mirrors
    `resolve_chat_model`). Ceiling, not override: a request at or below the cap
    (notably "off", the cheapest) is honored; only requests above it clamp
    down. `None`/unknown means the cap for free users, and "use the server
    default" (resolved in `build_model_settings`) for paid.
    """
    eff = (requested or "").strip().lower()
    if eff not in ALLOWED_REASONING_EFFORTS:
        eff = None  # unknown/absent -> tier default
    if tier != "paid":
        cap = FREE_TIER_EFFORT
        if eff is None or EFFORT_RANK[eff] > EFFORT_RANK[cap]:
            return cap
        return eff
    return eff


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
    if effort in _EFFORT_OFF:
        # Explicitly disable. Omitting the block is NOT off: hybrid-reasoning
        # models (DeepSeek V4) fall back to the provider default, which can be
        # reasoning ON — the "toggle Off but it still thinks" bug.
        body["reasoning"] = {"enabled": False}
    elif effort:
        body["reasoning"] = {"effort": effort}
    return {"extra_body": body}
