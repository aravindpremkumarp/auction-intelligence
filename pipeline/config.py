"""
pipeline/config.py
------------------
Shared configuration for the data intelligence pipeline.
Loads secrets from .env, defines paths and tuning parameters.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT_DIR      = Path(__file__).resolve().parent.parent
INPUT_JSONL   = ROOT_DIR / "tn_auction_data.jsonl"
DOWNLOADS_DIR = ROOT_DIR / "downloads"

PIPELINE_DIR  = ROOT_DIR / "pipeline"
CACHE_DIR     = PIPELINE_DIR / "cache" / "ocr_results"
CLASSIFY_CACHE_DIR = PIPELINE_DIR / "cache" / "classifications"
NOTICE_DESC_SINGLE_DIR = PIPELINE_DIR / "cache" / "notice_descriptions_v3"
NOTICE_DESC_MULTI_DIR  = PIPELINE_DIR / "cache" / "notice_descriptions_v3_multi"
OUTPUT_DIR    = PIPELINE_DIR / "output"
LOOKUPS_DIR   = PIPELINE_DIR / "lookups"
PROMPTS_DIR   = PIPELINE_DIR / "prompts"

# Ensure output/cache dirs exist
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CLASSIFY_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── OpenRouter ───────────────────────────────────────────────────────────────
# Two billing keys, one gateway. OPENROUTER_API_KEY funds the batch pipeline
# (scrape/OCR/classify/extract). OPENROUTER_CHAT_API_KEY funds the user-facing
# chat agent and carries its own, smaller credit cap on OpenRouter so a chat
# abuse spike can't drain the pipeline budget (or vice versa). The chat key
# falls back to the pipeline key when unset, so single-key setups keep working.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_CHAT_API_KEY = os.getenv("OPENROUTER_CHAT_API_KEY", "") or OPENROUTER_API_KEY
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Override via OPENROUTER_MODEL in .env. Verified options:
#   google/gemini-2.5-flash       (default; cheap, weaker at multi-turn grounding)
#   minimax/minimax-m2.5:free     (free tier; slower but decent grounding)
#   anthropic/claude-sonnet-4.5   (strongest tool use; paid)
# NB: google/gemini-2.0-flash-001 was retired from OpenRouter (404 "No
# endpoints found"); 2.5-flash is the drop-in successor.
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")

# Chat agent model — pinned separately from the pipeline model so the two can
# diverge. DeepSeek V4 Pro: 1M context, *automatic* prompt caching (the stable
# system+tools prefix is billed at the provider cache-hit rate — ~$0.003625/M
# vs ~$0.435/M cache-miss, ~99% off — and the cache persists long enough to
# survive bursty traffic, unlike Gemini implicit caching), plus reasoning.
# Override via OPENROUTER_MODEL_CHAT in .env.
OPENROUTER_MODEL_CHAT = os.getenv("OPENROUTER_MODEL_CHAT", "deepseek/deepseek-v4-pro")
# Cheaper, faster sibling offered alongside Pro as a user-selectable model.
# Flash trades reasoning depth for ~4-6x lower token cost and minimal reasoning
# output, so it's the default (and only) model for free/anonymous chat and an
# opt-in for paid users who want speed over depth. Same first-party DeepSeek
# provider as Pro, so the automatic prompt-cache assumption (and the provider
# pin below) applies equally. Override via OPENROUTER_MODEL_CHAT_FLASH in .env.
OPENROUTER_MODEL_CHAT_FLASH = os.getenv(
    "OPENROUTER_MODEL_CHAT_FLASH", "deepseek/deepseek-v4-flash"
)
# Reasoning effort for the chat model, sent via OpenRouter's `reasoning` param.
# deepseek-v4-pro supports "high" and "xhigh" (xhigh = max). Set to "off" (or
# empty) to disable. NB: reasoning tokens bill as output.
OPENROUTER_CHAT_REASONING_EFFORT = os.getenv("OPENROUTER_CHAT_REASONING_EFFORT", "high")
# Reasoning effort cap for free/anonymous chat. Reasoning tokens bill as output,
# so the global "high" default would let free users run the most expensive turns
# and drain the chat budget. Free/anon are clamped to this (server-enforced,
# ignores the client toggle); paid users keep the full range. Set to "off" for
# the cheapest possible free tier.
FREE_TIER_REASONING_EFFORT = os.getenv("FREE_TIER_REASONING_EFFORT", "low")

# Provider routing for the chat model, sent via OpenRouter's `provider` field.
# Without a pin, OpenRouter load-balances deepseek-v4-pro across third-party
# hosts (SiliconFlow, DigitalOcean, …) that charge ~3-4x first-party DeepSeek
# *and* cache far worse (~37% vs ~95% hit) — so the automatic prompt cache the
# chat agent relies on rarely lands and input cost balloons. Pin to first-party
# DeepSeek; comma-separated, in preference order. Empty disables the pin.
OPENROUTER_CHAT_PROVIDER_ORDER = os.getenv("OPENROUTER_CHAT_PROVIDER_ORDER", "deepseek")
# When the ordered provider(s) are unavailable: "true" lets OpenRouter fall back
# to other hosts (kept cheap by the price cap below), "false" fails the request.
OPENROUTER_CHAT_PROVIDER_ALLOW_FALLBACKS = os.getenv(
    "OPENROUTER_CHAT_PROVIDER_ALLOW_FALLBACKS", "true",
)
# Price ceiling ($/1M tokens) so any fallback stays on DeepSeek-class pricing —
# the default admits only deepseek/baidu/streamlake and excludes the ~3x
# SiliconFlow/DigitalOcean tier. Format "prompt,completion"; empty disables it.
OPENROUTER_CHAT_PROVIDER_MAX_PRICE = os.getenv(
    "OPENROUTER_CHAT_PROVIDER_MAX_PRICE", "0.9,1.8",
)

# Per-stage model overrides so the chat agent and the description pipeline
# can pin different models. Each defaults to a value that has been pilot-
# validated for that stage:
#   - SINGLE: gemini-2.5-flash (cheap, accurate on one-property notices)
#   - MULTI:  deepseek-v4-flash (non-reasoning sibling; clean per-lot splits)
#   - CLASSIFY: deepseek-v4-flash (single-shot single/multi judgment)
OPENROUTER_MODEL_DESCRIPTION_SINGLE = os.getenv(
    "OPENROUTER_MODEL_DESCRIPTION_SINGLE", OPENROUTER_MODEL,
)
OPENROUTER_MODEL_DESCRIPTION_MULTI = os.getenv(
    "OPENROUTER_MODEL_DESCRIPTION_MULTI", "deepseek/deepseek-v4-flash",
)
# LangExtract structured-extraction models, routed by notice type (see
# pipeline/extract_routing.select_extract_model, applied in load_extractions).
# Single-property notices are short and easy -> a cheap model; multi-property
# notices are long and must hold the per-lot structure together -> a stronger
# model. Both are OpenRouter slugs (the default LANGEXTRACT_PROVIDER); routing is
# skipped on the gemini-direct path.
OPENROUTER_MODEL_EXTRACT_SINGLE = os.getenv(
    "OPENROUTER_MODEL_EXTRACT_SINGLE", "tencent/hy3-preview",
)
OPENROUTER_MODEL_EXTRACT_MULTI = os.getenv(
    "OPENROUTER_MODEL_EXTRACT_MULTI", "deepseek/deepseek-v4-pro",
)
# Reasoning stays ON for extraction by default (empty list = suppress nothing):
# multi-lot disentangling benefits from the model thinking through which fields
# belong to which lot, and the cost is accepted. This is an OPT-IN cost lever —
# set it to comma-separated slug substrings (e.g. "deepseek") to force a
# hybrid-reasoning model's reasoning OFF ({"reasoning": {"enabled": false}}) on
# the copy-the-spans task if cost ever needs trimming.
LANGEXTRACT_REASONING_OFF_MODELS = os.getenv(
    "LANGEXTRACT_REASONING_OFF_MODELS", "",
)
OPENROUTER_MODEL_CLASSIFY = os.getenv(
    "OPENROUTER_MODEL_CLASSIFY", "deepseek/deepseek-v4-flash",
)
# Doc-type classifier for the dossier locker — places an uploaded user document
# into the 9-category / ~50-type taxonomy (api/dossier/taxonomy.py). Distinct
# from CLASSIFY (single/multi notice) because the taxonomy is much larger;
# gemini-2.5-flash is cheap and accurate on this kind of label-selection task.
OPENROUTER_MODEL_DOC_CLASSIFY = os.getenv(
    "OPENROUTER_MODEL_DOC_CLASSIFY", "google/gemini-2.5-flash",
)

# ── Web search (Tavily) ──────────────────────────────────────────────────────
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# ── Neo4j ────────────────────────────────────────────────────────────────────
# Credentials are stored in .env under CLIENT_ID / CLIENT_SECRET / CLIENT_NAME
# (Neo4j Aura instance ID doubles as username and database name).
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME") or os.getenv("CLIENT_ID", "")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD") or os.getenv("CLIENT_SECRET", "")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE") or os.getenv("CLIENT_NAME", "") or NEO4J_USERNAME

NEO4J_URI = os.getenv("NEO4J_URI") or (
    f"neo4j+s://{NEO4J_USERNAME}.databases.neo4j.io" if NEO4J_USERNAME else ""
)

# ── Neo4j connection-pool tuning ─────────────────────────────────────────────
# Aura (and its load balancer) silently close Bolt connections that have been
# idle for a few minutes. The driver's defaults — liveness_check_timeout=None
# (idle pooled connections are never probed before reuse) and
# max_connection_lifetime=3600s — mean the first request after an idle gap can
# hand a query a dead connection and raise
# SessionExpired("Failed to read from defunct connection"). On this deploy the
# 5s /health pings don't touch Neo4j, so real requests are sparse and the pool
# sits idle between them. A liveness probe on idle connections plus a shorter
# max lifetime make the pool self-heal instead of surfacing the drop as a 500
# (auth/me) or "chat agent failed" (the agent's Neo4j tool calls).
# LIVENESS is the primary knob: any connection idle longer than it is RESET-
# probed before reuse and discarded if dead; MAX_LIFETIME bounds total age as
# defense in depth. Both are seconds; env-tunable without a redeploy.
NEO4J_LIVENESS_CHECK_TIMEOUT_S = float(
    os.getenv("NEO4J_LIVENESS_CHECK_TIMEOUT_S", "30")
)
NEO4J_MAX_CONNECTION_LIFETIME_S = float(
    os.getenv("NEO4J_MAX_CONNECTION_LIFETIME_S", "600")
)
NEO4J_CONNECTION_ACQUISITION_TIMEOUT_S = float(
    os.getenv("NEO4J_CONNECTION_ACQUISITION_TIMEOUT_S", "60")
)
# How many times to retry a query that fails with a transient Neo4j error
# (SessionExpired / ServiceUnavailable / TransientError) before giving up. The
# production failures fail at connection-acquisition time (an idle-dropped
# connection detected on acquire, or a routing-table refresh against dead
# connections), so nothing has executed yet and re-acquiring on a fresh
# connection succeeds. Attempts total = retries + 1. Belt to the liveness-check
# suspenders: the liveness probe prevents most drops, the retry catches the
# residual race where a connection dies between the probe and the query.
NEO4J_MAX_QUERY_RETRIES = int(os.getenv("NEO4J_MAX_QUERY_RETRIES", "2"))
NEO4J_RETRY_BASE_DELAY_S = float(os.getenv("NEO4J_RETRY_BASE_DELAY_S", "0.2"))

# ── Tuning ───────────────────────────────────────────────────────────────────
BATCH_SIZE       = 10    # concurrent LLM calls
MAX_RETRIES      = 3
RATE_LIMIT_DELAY = 0.5   # seconds between batches
NEO4J_BATCH_SIZE = 100   # records per Neo4j transaction
PILOT_SIZE       = int(os.getenv("PILOT_SIZE", "50"))

# ── Dossier ingest caps (sync-with-caps upload path) ─────────────────────────
# Uploads are OCR'd + classified synchronously inside the request, so the caps
# keep a single request bounded (no new queue/worker infra). Tune via .env.
DOSSIER_MAX_FILE_MB = int(os.getenv("DOSSIER_MAX_FILE_MB", "10"))
DOSSIER_MAX_PAGES   = int(os.getenv("DOSSIER_MAX_PAGES", "15"))

# ── Scoring weights (Phase 2) ─────────────────────────────────────────────────
SCORING_WEIGHTS = {
    "price_attractiveness": 0.20,
    "location_quality":     0.15,
    "legal_clarity":        0.15,
    "bank_reliability":     0.10,
    "property_condition":   0.10,
    "timeline_urgency":     0.10,
    "due_diligence_ease":   0.05,
    "area_price_trend":     0.05,
    "competition_risk":     0.05,
    "yield_potential":      0.05,
}

DECISION_THRESHOLDS = {
    "strong_buy":     85,   # A / A+
    "worth_pursuing": 70,   # B
    "selective":      55,   # C
    # below → skip
}

# ── Auction portals (Phase 5) ────────────────────────────────────────────────
PORTALS = {
    "eauctions_india":  "https://www.eauctionsindia.com",
    "ibapi":            "https://ibapi.in",
    "bankauctions_in":  "https://bankauctions.in",
    "bankeauctions":    "https://bankeauctions.com",
    "findauction":      "https://www.findauction.in",
}

# ── Output paths (Phase 3-4) ────────────────────────────────────────────────
TRACKING_TSV   = ROOT_DIR / "tracking" / "auction_pipeline.tsv"
REPORTS_DIR    = ROOT_DIR / "reports" / "output"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
