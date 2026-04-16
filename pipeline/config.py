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
OUTPUT_DIR    = PIPELINE_DIR / "output"
LOOKUPS_DIR   = PIPELINE_DIR / "lookups"
PROMPTS_DIR   = PIPELINE_DIR / "prompts"

# Ensure output/cache dirs exist
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── OpenRouter ───────────────────────────────────────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Override via OPENROUTER_MODEL in .env. Verified options:
#   google/gemini-2.0-flash-001   (default; cheap, weaker at multi-turn grounding)
#   minimax/minimax-m2.5:free     (free tier; slower but decent grounding)
#   anthropic/claude-sonnet-4.5   (strongest tool use; paid)
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.0-flash-001")

# ── Neo4j ────────────────────────────────────────────────────────────────────
# Credentials are stored in .env under CLIENT_ID / CLIENT_SECRET / CLIENT_NAME
# (Neo4j Aura instance ID doubles as username and database name).
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME") or os.getenv("CLIENT_ID", "")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD") or os.getenv("CLIENT_SECRET", "")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE") or os.getenv("CLIENT_NAME", "") or NEO4J_USERNAME

NEO4J_URI = os.getenv("NEO4J_URI") or (
    f"neo4j+s://{NEO4J_USERNAME}.databases.neo4j.io" if NEO4J_USERNAME else ""
)

# ── Tuning ───────────────────────────────────────────────────────────────────
BATCH_SIZE       = 10    # concurrent LLM calls
MAX_RETRIES      = 3
RATE_LIMIT_DELAY = 0.5   # seconds between batches
NEO4J_BATCH_SIZE = 100   # records per Neo4j transaction
PILOT_SIZE       = int(os.getenv("PILOT_SIZE", "50"))

# ── Scoring weights (Phase 2) ────────────────────────────────────────────────
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
