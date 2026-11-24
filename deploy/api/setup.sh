#!/usr/bin/env bash
# Install / update auction-api on the Oracle VM (replaces the Render web service).
#
#   sudo bash setup.sh [BRANCH]          # BRANCH defaults to main
#
# Run 1 creates /opt/auction-api/api.env with the Neo4j values filled in and
# the other secrets blank, then stops so you can fill them (sudo nano).
# Run 2+ builds, starts, and (once) adds the api.auctionscope.in site to the
# existing Caddy. Re-running later = deploy the latest code.
#
# Changes outside /opt/auction-api: appends one site block to Caddy's
# Caddyfile (after a backup, validated before reload, restored on failure).
# Nothing else in the Postiz stack is touched.
set -euo pipefail

BRANCH="${1:-main}"
DIR=/opt/auction-api
REPO=https://github.com/aravindpremkumarp/auction-intelligence.git
API_DOMAIN="${API_DOMAIN:-api.auctionscope.in}"

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
[ -f /opt/neo4j/auth.env ] || { echo "Neo4j is not installed (/opt/neo4j/auth.env missing)." >&2; exit 1; }
docker network inspect neo4j_default >/dev/null 2>&1 || { echo "Docker network neo4j_default missing; is Neo4j running?" >&2; exit 1; }

mkdir -p "$DIR"
cd "$DIR"

# ── 1. Code ─────────────────────────────────────────────────────────────────
if [ -d src/.git ]; then
  echo "==> Updating code ($BRANCH)"
  git -C src fetch -q --depth 1 origin "$BRANCH"
  git -C src checkout -q -B "$BRANCH" FETCH_HEAD
else
  echo "==> Cloning code ($BRANCH)"
  git clone -q --depth 1 --branch "$BRANCH" "$REPO" src
fi
install -m 644 src/deploy/api/docker-compose.yml docker-compose.yml
echo "   at commit $(git -C src rev-parse --short HEAD)"

# ── 2. Find the Caddy that serves Postiz ────────────────────────────────────
CADDY="$(docker ps --format '{{.Names}} {{.Image}}' | awk '$2 ~ /caddy/ {print $1}')"
[ "$(echo "$CADDY" | grep -c .)" -eq 1 ] || { echo "Expected exactly one running Caddy container, found: '${CADDY}'" >&2; exit 1; }
CADDY_NETWORK="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$CADDY" | awk '{print $1}')"
CADDYFILE="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}' "$CADDY")"
if [ -z "$CADDYFILE" ]; then
  CADDY_DIR="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/etc/caddy"}}{{.Source}}{{end}}{{end}}' "$CADDY")"
  [ -n "$CADDY_DIR" ] && CADDYFILE="$CADDY_DIR/Caddyfile"
fi
[ -n "$CADDYFILE" ] && [ -f "$CADDYFILE" ] || { echo "Could not find Caddy's Caddyfile on the host (container $CADDY)." >&2; exit 1; }
echo "==> Caddy: container=$CADDY network=$CADDY_NETWORK file=$CADDYFILE"
printf 'CADDY_NETWORK=%s\n' "$CADDY_NETWORK" > .env

# ── 3. Secrets file ─────────────────────────────────────────────────────────
if [ ! -f api.env ]; then
  NEO4J_PW="$(cut -d/ -f2 /opt/neo4j/auth.env)"
  cat > api.env <<EOF
# auction-api settings. Copy each blank value from Render (auction-api ->
# Environment). Leave a line blank only if it is blank/absent on Render too.

# Filled automatically: the local Neo4j over the Docker network.
NEO4J_URI=bolt+ssc://neo4j:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=$NEO4J_PW
NEO4J_DATABASE=neo4j

APP_ENV=prod
AUTH_ENABLED=true
RAZORPAY_PLAN_AMOUNT=49900
RAZORPAY_PLAN_CURRENCY=INR
RAZORPAY_PLAN_DAYS=30
RAZORPAY_WEBHOOK_TTL_DAYS=90

# Copy from Render:
OPENROUTER_API_KEY=
OPENROUTER_MODEL=
OPENAI_API_KEY=
GOOGLE_API_KEY=
FEEDBACK_RESOLVE_TOKEN=
SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=
APP_BASE_URL=
ADMIN_BOOTSTRAP_EMAIL=
R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET=
R2_PUBLIC_BASE_URL=
MINERU_API_KEY=
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
RAZORPAY_WEBHOOK_SECRET=
DATALAB_API_KEY=
LOGFIRE_TOKEN=
OPENROUTER_CHAT_API_KEY=
TAVILY_API_KEY=
FREE_TIER_REASONING_EFFORT=
LOGFIRE_ENVIRONMENT=
OPENROUTER_CHAT_PROVIDER_ORDER=
EOF
  chmod 600 api.env
  echo
  echo "Created $DIR/api.env. Fill in the blank values from Render, then run this script again:"
  echo "  sudo nano $DIR/api.env"
  exit 0
fi
chmod 600 api.env
EMPTY="$(grep -E '^[A-Z_]+=$' api.env | cut -d= -f1 | tr '\n' ' ')"
[ -z "$EMPTY" ] || echo "   NOTE: still blank in api.env: $EMPTY"

# ── 4. Build + start ────────────────────────────────────────────────────────
echo "==> Building image (first build takes a few minutes)"
docker compose build -q
docker compose up -d

echo -n "==> Waiting for /health"
for _ in $(seq 1 60); do
  STATE="$(docker inspect -f '{{.State.Health.Status}}' auction-api 2>/dev/null || echo starting)"
  [ "$STATE" = healthy ] && { echo " ok"; break; }
  if [ "$(docker inspect -f '{{.State.Status}}' auction-api)" != running ]; then
    echo; docker logs --tail 40 auction-api >&2; exit 1
  fi
  echo -n "."; sleep 5
done
[ "$STATE" = healthy ] || { echo; echo "Not healthy yet; check: docker logs --tail 80 auction-api" >&2; exit 1; }

# ── 5. Caddy site (once) ────────────────────────────────────────────────────
if grep -q "^$API_DOMAIN" "$CADDYFILE"; then
  echo "==> Caddy already serves $API_DOMAIN"
else
  echo "==> Adding $API_DOMAIN to Caddy"
  BACKUP="$CADDYFILE.bak-$(date +%Y%m%d-%H%M%S)"
  cp "$CADDYFILE" "$BACKUP"
  # Append in place (keeps the inode, so a single-file bind mount sees it).
  cat >> "$CADDYFILE" <<EOF

$API_DOMAIN {
	encode gzip
	reverse_proxy auction-api:8000
}
EOF
  if docker exec "$CADDY" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 \
     && docker exec "$CADDY" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile; then
    echo "   Caddy reloaded (backup: $BACKUP)"
  else
    cat "$BACKUP" > "$CADDYFILE"
    echo "Caddy rejected the new site; Caddyfile restored from $BACKUP." >&2
    exit 1
  fi
fi

cat <<EOF

Done. auction-api is running.
  Local check : docker exec auction-api python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health').read())"
  Public URL  : https://$API_DOMAIN/health  (works once DNS points $API_DOMAIN to this VM)
  Logs        : docker logs --tail 100 auction-api
  Deploy new code later: sudo bash $DIR/src/deploy/api/setup.sh main
EOF
