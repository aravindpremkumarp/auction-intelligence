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
DOWNLOADS_DIR = ROOT_DIR / "downloads"

PIPELINE_DIR  = ROOT_DIR / "pipeline"
LOOKUPS_DIR   = PIPELINE_DIR / "lookups"
PROMPTS_DIR   = PIPELINE_DIR / "prompts"

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

# LangExtract structured-extraction models, routed by notice type (see
# pipeline/extract_routing.select_extract_model, applied in load_extractions).
# Single-property notices are short and easy -> a cheap model; multi-property
# notices are long and must hold the per-lot structure together -> a stronger
# model. Both are OpenRouter slugs (the default LANGEXTRACT_PROVIDER); routing is
# skipped on the gemini-direct path.
#
# `tencent/hy3-preview` held this slot until it was measured against the 238
# single-lot notices the `--refresh` sweep selected: 27 of 236 came back
# unparseable ("Content must contain an 'extractions' key" — every chunk
# skipped, so zero entities), and single-lot documents scoring under 60 rose
# from 20 to 37. It is not that those notices are hard; the two DeepSeek models
# read the same set with no parse failure at all (flash-0731 on 8, pro-0813 on
# 53), and pro-0813 took the corpus average to 87.9 with 6 documents left below
# 60. flash-0731 is the cheaper of the two by 17x ($0.065/$0.18 per M tokens
# against $1.12/$3.36) and cheaper than hy3-preview as well, with 5x its
# context; it holds this slot until its clean record is contradicted on a
# larger sample than the 8 documents it has so far.
#
# Both slots now name what OpenRouter's "Latest" aliases resolve to today —
# `~deepseek/deepseek-flash-latest` -> v4.1-flash, `~deepseek/deepseek-pro-latest`
# -> v4-pro-0813 — written out dated rather than as the alias. An alias moves
# under us on the provider's schedule, and `Document.extraction_model` stamps
# the slug we asked for: a score change would then be unattributable, which is
# the one thing that stamp exists to prevent.
#
# The multi slot in particular was NOT the model it was measured on. The
# un-dated `deepseek/deepseek-v4-pro` is V4 Pro 0423, while the 87.9 average
# above was pro-0813: the corpus's multi-lot extractions average 67 on 0423,
# which is the older model at 2.8x the price ($1.60/$3.20 per M tokens against
# $0.58/$1.74). Naming the date is what keeps the two apart.
OPENROUTER_MODEL_EXTRACT_SINGLE = os.getenv(
    "OPENROUTER_MODEL_EXTRACT_SINGLE", "deepseek/deepseek-v4.1-flash",
)
# Both slots are Flash. Pro held the multi slot for one run and answered 15 of
# its 68 multi-lot pages with no content at all — sometimes as an API error,
# more often as a silent zero-entity result that was written to the graph and
# marked done. Reasoning off did not change it, and the same pages extracted
# cleanly on Flash (one went from 0 entities to 101). A model that drops a
# fifth of the hardest documents is not the stronger model for them, whatever
# it scores on the ones it does answer.
OPENROUTER_MODEL_EXTRACT_MULTI = os.getenv(
    "OPENROUTER_MODEL_EXTRACT_MULTI", "deepseek/deepseek-v4.1-flash",
)
# The "stronger model" rung of pipeline/lot_chunks' retry ladder: it only ever
# re-reads the few lots a Flash read of a small excerpt left out. Pro's failure
# mode above (an empty answer on a long page) costs one call there and nothing
# else — a retry that finds nothing adds nothing and replaces nothing.
OPENROUTER_MODEL_EXTRACT_RETRY = os.getenv(
    "OPENROUTER_MODEL_EXTRACT_RETRY", "deepseek/deepseek-v4-pro",
)
# Reasoning is OFF for extraction. It was on, as a quality choice, until the
# empty responses were traced to it: reasoning tokens are spent from the SAME
# output budget as the answer, so a model that thinks too long returns no
# content at all. That is the whole of "OpenAI response contained no message
# content" — 3.5% of one 453-page run, ~22% of a pro-0813 run, always on the
# documents with most to think about.
#
# A one-token probe shows the mechanism on its own: ask v4.1-flash to "say ok"
# with max_tokens=10 and it spends all ten reasoning and returns nothing
# (finish_reason=length); at 100 it reasons for 48 and answers.
#
# WHAT THIS DOES AND DOES NOT BUY, measured on the 589-page run that followed
# (batch B58), against the 453-page run before it (B56, reasoning on):
#
#   failures   2.7% here vs 3.5% there — NOT the fix it first looked like. The
#              19 pages that failed with reasoning all came back when re-run
#              without it, but re-running is itself most of that: the empty
#              response moved rather than stopped, from "no message content"
#              to a parsed result with no entities at all (7 of the 12 here;
#              the other 5 were network drops). Both are caught and left
#              pending, so neither loses a page.
#   speed      real: 6/min at the start of B58 against 2.5/min in B56,
#              settling near 3/min on the multi-lot tail.
#   score      single 91 vs 93, multi 73 vs 82 — but B58 is the past-auction
#              tail, older and dirtier scans, so how much of that gap is the
#              setting and how much is the corpus is NOT established. The
#              clean test is the same pages both ways; it has not been run.
#
# So: keep it for speed and cost, not for reliability, and do not let the
# score gap above be quoted as settled either way.
#
# Comma-separated slug substrings; empty re-enables reasoning everywhere.
LANGEXTRACT_REASONING_OFF_MODELS = os.getenv(
    "LANGEXTRACT_REASONING_OFF_MODELS", "deepseek",
)
# Doc-type classifier for the dossier locker — places an uploaded user document
# into the 9-category / ~50-type taxonomy (api/dossier/taxonomy.py);
# gemini-2.5-flash is cheap and accurate on this kind of label-selection task.
OPENROUTER_MODEL_DOC_CLASSIFY = os.getenv(
    "OPENROUTER_MODEL_DOC_CLASSIFY", "google/gemini-2.5-flash",
)

# ── OCR engine (notice layout extraction) ────────────────────────────────────
# Which backend the bulk pipeline (scripts/ocr_with_mineru.py stage 1) uses to
# turn a notice image/PDF into layout-aware markdown + blocks. "datalab" is the
# default (faster + cleaner on the raster notices that sent MinerU into
# repetition loops); "mineru" keeps the original hosted MinerU path. This only
# governs the BULK pipeline — the annotator's per-block re-extract picks its
# engine per request (reviewer's choice), independent of this flag.
DESCRIPTION_OCR_ENGINE = os.getenv("DESCRIPTION_OCR_ENGINE", "datalab").strip().lower()
# Datalab tier routing by notice_type. Multi-property notices are long/dense and
# the "fast" tier can silently drop them, so they default to "accurate"; single
# notices stay on "fast" (≈5x faster, plenty for one property). Both env-tunable.
DATALAB_MODE_SINGLE = os.getenv("DATALAB_MODE_SINGLE", "fast").strip().lower()
DATALAB_MODE_MULTI  = os.getenv("DATALAB_MODE_MULTI", "accurate").strip().lower()
# Concurrent Datalab calls in the bulk stage (per-file, unlike MinerU's batches).
DATALAB_PIPELINE_CONCURRENCY = int(os.getenv("DATALAB_PIPELINE_CONCURRENCY", "4"))
# How long a reviewer's re-ingest waits on one Datalab job before giving up.
# The client's 300s default threw away finished work: the accurate tier took
# 359s and 933s on one newspaper notice (2026-09-15). A timeout discards the
# result, so err long — the annotator polls for longer than this (review.html).
DATALAB_REINGEST_TIMEOUT_S = int(os.getenv("DATALAB_REINGEST_TIMEOUT_S", "1200"))


def datalab_mode_for(notice_type: str | None) -> str:
    """Datalab tier for a notice_type: multi → accurate, everything else → fast."""
    return DATALAB_MODE_MULTI if (notice_type or "").strip().lower() == "multi" \
        else DATALAB_MODE_SINGLE


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
MAX_RETRIES      = 3     # per-request retries against OpenRouter
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
