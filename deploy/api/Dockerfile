# auction-api for the Oracle ARM VM (replaces the Render web service).
# Build context is the repo root: docker build -f deploy/api/Dockerfile .
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so code-only changes reuse this layer.
COPY requirements.lock .
RUN pip install -r requirements.lock

COPY . .

RUN useradd --system --uid 10001 app
USER app

EXPOSE 8000

# init_auth_schema = Render's preDeployCommand (idempotent IF NOT EXISTS).
# --proxy-headers + --forwarded-allow-ips='*': the client IP comes from
# Caddy's X-Forwarded-For (anon chat quota is keyed on it). Safe because the
# port is never published on the host; only Caddy's network reaches it.
CMD ["sh", "-c", "python -m scripts.init_auth_schema && exec uvicorn api.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
