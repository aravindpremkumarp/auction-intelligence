"""
api/telemetry.py
----------------
Optional distributed tracing via Pydantic Logfire.

`pydantic-ai` is natively instrumented with OpenTelemetry, so turning Logfire
on captures a full trace of every chat turn — the agent run, each LLM request
(with prompt/response, token counts, and cost), and every tool call — viewable
as a waterfall in the Logfire UI. This is the "observability" half of the
LangSmith-style setup; the "evaluation" half lives in the `evals/` package.

Design mirrors `api/observability.py`: it is a **no-op when unconfigured** so
imports never fail and local/dev/test runs need no token. Tracing only switches
on when ``LOGFIRE_TOKEN`` is set. Because the underlying transport is OTLP, the
same instrumentation can be pointed at any OpenTelemetry backend (LangSmith,
Langfuse, Honeycomb, …) via the standard ``OTEL_EXPORTER_OTLP_*`` env vars
without touching this file.

Call :func:`configure_telemetry` once at process startup (see api/main.py).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("auction.obs")

_configured = False


def telemetry_enabled() -> bool:
    """True when a Logfire token (or a generic OTLP endpoint) is configured.

    A bare ``OTEL_EXPORTER_OTLP_ENDPOINT`` lets an operator ship traces to a
    non-Logfire backend without a Logfire token, so we honour either signal.
    """
    return bool(
        os.getenv("LOGFIRE_TOKEN")
        or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    )


def configure_telemetry(app: object | None = None) -> bool:
    """Configure Logfire + pydantic-ai/FastAPI/HTTPX instrumentation.

    Returns True when tracing was switched on, False when it stayed a no-op
    (no token configured, or the optional ``logfire`` dependency is missing).
    Idempotent: safe to call more than once.

    Parameters
    ----------
    app:
        The FastAPI app to instrument for request spans. Optional so this
        module has no hard dependency on FastAPI and can be called from the
        eval runner (which has no web app) just for agent/LLM tracing.
    """
    global _configured
    if _configured:
        return True
    if not telemetry_enabled():
        logger.debug("telemetry: LOGFIRE_TOKEN unset — tracing disabled (no-op)")
        return False
    try:
        import logfire
    except ImportError:
        # logfire is in requirements.txt, but guard so a stripped-down install
        # (or a dev box that skipped it) degrades to the structured-log path in
        # api/observability.py instead of crashing startup.
        logger.warning("telemetry: logfire not installed — tracing disabled")
        return False

    # send_to_logfire="if-token-present" means a missing token quietly disables
    # the Logfire exporter while still wiring up OTel (so OTEL_* env exporters,
    # e.g. a LangSmith/Langfuse OTLP endpoint, keep working).
    logfire.configure(
        token=os.getenv("LOGFIRE_TOKEN") or None,
        service_name=os.getenv("LOGFIRE_SERVICE_NAME", "auction-intelligence"),
        environment=os.getenv("LOGFIRE_ENVIRONMENT") or os.getenv("APP_ENV", "dev"),
        send_to_logfire="if-token-present",
        console=False,  # don't duplicate spans onto stdout in prod logs
    )
    # Captures the agent run + every model request (prompt, response, tokens,
    # cost) and tool call as spans — this is the core of the trace.
    logfire.instrument_pydantic_ai()

    # Ship our own structured log lines into Logfire. `auction.obs` carries
    # api/observability.py's `timed()` and `record()` output — chat latency,
    # per-turn token counts, per-tool-call timings, Neo4j query timings.
    # `api` carries everything the application's own modules log, which is how
    # a `logger.exception` from a router (e.g. "chat agent3 stream failed")
    # becomes visible.
    #
    # **Both, not just the first, and that is the fix here.** Attaching only
    # `auction.obs` meant every INFO line from an `api.*` module reached no
    # handler at all: nothing configures root logging, uvicorn only configures
    # its own loggers, so those records fell through to `logging.lastResort`
    # and were dropped below WARNING. The agent3 endpoint had been logging
    # `in_tok`/`cached_tok`/`out_tok` per turn since it shipped and not one of
    # those lines was ever stored anywhere.
    #
    # Still not the root logger: that would sweep in every library's INFO
    # chatter (httpx request lines, neo4j pool churn) and bury the signal.
    # setLevel is required on each — without an explicit level they inherit
    # root's WARNING and the happy-path lines never reach the handler.
    from logfire.integrations.logging import LogfireLoggingHandler

    handler = LogfireLoggingHandler()
    for name in ("auction.obs", "api"):
        app_logger = logging.getLogger(name)
        app_logger.setLevel(logging.INFO)
        app_logger.addHandler(handler)

    # Every OpenRouter call is an httpx request, so this is what puts the
    # individual model calls on the turn's waterfall with their own latency.
    # Bodies are not captured: prompts and answers would multiply the export
    # volume and carry user text off-box for no diagnostic gain that the
    # token counts don't already give.
    try:
        logfire.instrument_httpx()
    except Exception as exc:  # noqa: BLE001 - never fail startup over tracing
        logger.warning(
            "telemetry: HTTPX instrumentation skipped (%s) — model-call "
            "latency will not appear as spans", exc,
        )

    if app is not None:
        # Root HTTP span per request, so a /chat trace shows the full
        # request → agent → LLM/tool waterfall under one trace id. Needs the
        # logfire[fastapi] extra; if it's missing, keep the (more valuable)
        # agent/LLM tracing rather than failing app startup.
        try:
            logfire.instrument_fastapi(app, capture_headers=False)
        except (ImportError, RuntimeError) as exc:
            logger.warning(
                "telemetry: FastAPI instrumentation skipped (%s) — "
                "install logfire[fastapi] for per-request spans", exc,
            )

    _configured = True
    logger.info(
        "telemetry: Logfire tracing enabled (env=%s)",
        os.getenv("LOGFIRE_ENVIRONMENT") or os.getenv("APP_ENV", "dev"),
    )
    return True
