"""
api/main.py
-----------
FastAPI composition root: CORS + rate-limit middleware, exception handlers,
router wiring, and static UI serving. The endpoint logic lives in focused
routers (api/chat, api/properties, api/feedback, api/health, plus the auth /
watchlist / conversations / review subpackages) so this file stays a thin
assembly layer.

Run with: uvicorn api.main:app --reload
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded

from api.alerts import router as alerts_router
from api.auth import router as auth_router
from api.auth.rate_limit import limiter
from api.chat import router as chat_router
from api.conversations import router as conversations_router
from api.dossier import router as dossier_router
from api.feedback import router as feedback_router
from api.health import router as health_router
from api.properties import router as properties_router
from api.review import router as review_router
from api.telemetry import configure_telemetry
from api.watchlist import router as watchlist_router

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"


def _validate_required_env() -> None:
    """Fail fast at boot with a clear message instead of a mid-request crash.

    Only enforced outside dev/test so offline development (AUTH_ENABLED=false,
    stubbed graph) keeps working without a full .env.
    """
    if os.environ.get("APP_ENV", "prod").lower() in {"dev", "test"}:
        return
    required = ["NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "OPENROUTER_API_KEY"]
    if os.environ.get("AUTH_ENABLED", "true").lower() not in {"false", "0", "no"}:
        required += ["SUPABASE_URL", "SUPABASE_ANON_KEY"]
    missing = [v for v in required if not os.environ.get(v, "").strip()]
    if missing:
        raise RuntimeError(f"missing required environment variables: {', '.join(missing)}")


_validate_required_env()

app = FastAPI(title="Bank Auction Intelligence API", version="0.1.0")

# Optional Logfire/OpenTelemetry tracing — no-op unless LOGFIRE_TOKEN (or a
# generic OTLP endpoint) is set. Instruments pydantic-ai (agent + LLM + tool
# spans) and FastAPI (per-request root span) so a /chat turn shows a full
# trace waterfall. See api/telemetry.py.
configure_telemetry(app)


# Branded site + Vercel previews are matched by regex; explicit dev/prod
# origins come from _cors_allow_list(). Kept as a module constant so the
# CORSMiddleware config and the error-path origin check stay in sync.
_CORS_ORIGIN_REGEX = r"https://(.*\.vercel\.app|(.*\.)?auctionscope\.in)"
_cors_origin_re = re.compile(_CORS_ORIGIN_REGEX)


def _cors_allow_list() -> list[str]:
    base = os.environ.get("APP_BASE_URL", "").strip()
    env = os.environ.get("APP_ENV", "prod").lower()
    if env in {"dev", "test"}:
        return ["*"]
    origins = {"http://localhost:5173", "http://localhost:3000"}
    if base:
        origins.add(base.rstrip("/"))
    return sorted(origins)


def _origin_allowed(origin: str | None) -> bool:
    """Mirror the CORSMiddleware policy so error responses — which are emitted
    by ServerErrorMiddleware *outside* CORSMiddleware and therefore never get
    its headers — can echo a valid Access-Control-Allow-Origin."""
    if not origin:
        return False
    allow = _cors_allow_list()
    if "*" in allow or origin in allow:
        return True
    return bool(_cors_origin_re.fullmatch(origin))


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_list(),
    allow_origin_regex=_CORS_ORIGIN_REGEX,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
)

# Rate limiter (slowapi) — shared across auth + anonymous chat throttles.
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": "rate limit exceeded"})


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # The catch-all Exception handler is invoked by Starlette's
    # ServerErrorMiddleware, which sits OUTSIDE CORSMiddleware — so (unlike
    # HTTPException responses) this 500 never passes back through CORS and
    # would reach the browser with no Access-Control-Allow-Origin. The browser
    # then discards it and the fetch rejects as "Failed to fetch", masking the
    # real status. Re-attach the CORS header here so the SPA can surface a
    # clear error (e.g. "detail 500") instead of a generic network failure.
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    resp = JSONResponse(status_code=500, content={"detail": "internal server error"})
    origin = request.headers.get("origin")
    if _origin_allowed(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    return resp


# Always-on public routers. `/alerts` is public so the anonymous POST path
# (client-supplied watchlist ids) works even when the auth-gated watchlist
# router below is disabled; its GET path resolves the saved set only when a
# valid token is present and returns an empty list otherwise.
app.include_router(health_router)
app.include_router(properties_router)
app.include_router(chat_router)
app.include_router(feedback_router)
app.include_router(alerts_router)

# Auth-gated routers — skipped entirely when AUTH_ENABLED=false (local/offline
# dev) so the app can boot without Supabase configured.
if os.environ.get("AUTH_ENABLED", "true").lower() != "false":
    app.include_router(auth_router)
    app.include_router(watchlist_router)
    app.include_router(conversations_router)
    app.include_router(review_router)
    app.include_router(dossier_router)


# Serve the single-page UI. index.html links /styles.css and /app.js as plain
# top-level assets; on Vercel they're served straight from the filesystem,
# and these explicit routes make `uvicorn api.main:app` (local dev + Render)
# serve them too.
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    def root() -> FileResponse:
        return FileResponse(str(WEB_DIR / "index.html"))

    @app.get("/styles.css")
    def styles_css() -> FileResponse:
        return FileResponse(str(WEB_DIR / "styles.css"), media_type="text/css")

    @app.get("/app.js")
    def app_js() -> FileResponse:
        return FileResponse(str(WEB_DIR / "app.js"), media_type="application/javascript")

    @app.get("/auth.js")
    def auth_js() -> FileResponse:
        return FileResponse(str(WEB_DIR / "auth.js"), media_type="application/javascript")

    @app.get("/dossiers.js")
    def dossiers_js() -> FileResponse:
        return FileResponse(str(WEB_DIR / "dossiers.js"), media_type="application/javascript")

    @app.get("/admin")
    def admin_page() -> FileResponse:
        return FileResponse(str(WEB_DIR / "admin.html"))

    @app.get("/review")
    def review_page() -> FileResponse:
        return FileResponse(str(WEB_DIR / "review.html"))

    # SPA deep-link fallbacks. The client router (web/index.html) pushes
    # `/chat` and `/property/{id}`; on a fresh load or refresh the browser
    # GETs those paths and the server must hand back index.html so the SPA
    # boots and its own router renders the right screen. In production a CDN
    # rewrite covers this, but the dev server (and any non-rewriting host)
    # needs explicit routes or refresh dies on a JSON 404/405.
    #   - GET /chat does NOT collide with the chat API (that's POST /chat).
    #   - GET /property/{id} has no API route at this path.
    #   - /watchlist is intentionally NOT here: GET /watchlist is the
    #     authenticated data API, so an HTML fallback would shadow it.
    @app.get("/chat")
    def chat_page() -> FileResponse:
        return FileResponse(str(WEB_DIR / "index.html"))

    @app.get("/property/{auction_id}")
    def property_page(auction_id: str) -> FileResponse:
        return FileResponse(str(WEB_DIR / "index.html"))
