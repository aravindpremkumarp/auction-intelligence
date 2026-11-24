#!/usr/bin/env bash
# One-time (and safe to re-run) install of Neo4j Community on the Oracle VM.
#
#   sudo bash setup.sh [PUBLIC_HOST]
#
# PUBLIC_HOST defaults to the VM's public IP. Re-running never changes an
# existing password, certificate or data; it only re-applies config.
#
# Does NOT touch the Postiz stack (/opt/postiz) or any other container.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR=/opt/neo4j
NEO4J_UID=7474   # uid the official neo4j image runs as

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
command -v docker >/dev/null || { echo "Docker is not installed." >&2; exit 1; }
docker compose version >/dev/null || { echo "Docker Compose v2 is missing." >&2; exit 1; }

PUBLIC_HOST="${1:-}"
if [ -z "$PUBLIC_HOST" ] && [ -f "$DIR/.env" ]; then
  PUBLIC_HOST="$(grep -E '^NEO4J_PUBLIC_HOST=' "$DIR/.env" | cut -d= -f2-)"
fi
if [ -z "$PUBLIC_HOST" ]; then
  PUBLIC_HOST="$(curl -fsS https://ifconfig.me || true)"
fi
[ -n "$PUBLIC_HOST" ] || { echo "Could not detect the public IP; pass it: sudo bash setup.sh <ip>" >&2; exit 1; }
echo "==> Public host: $PUBLIC_HOST"

# ── 1. Swap (the VM ships with none; a memory spike would kill containers) ──
if [ "$(swapon --noheadings | wc -l)" -eq 0 ]; then
  echo "==> Adding 4 GB swap"
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -q vm.swappiness=10
  grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
else
  echo "==> Swap already present, skipping"
fi

# ── 2. Folders + config ─────────────────────────────────────────────────────
mkdir -p "$DIR"/{data,logs,backups,certs}
install -m 644 "$SRC_DIR/docker-compose.yml" "$DIR/docker-compose.yml"
install -m 755 "$SRC_DIR/backup.sh" "$DIR/backup.sh"

# .env: compose interpolation only. auth.env: the password, read by the
# container directly (see docker-compose.yml).
printf 'NEO4J_PUBLIC_HOST=%s\n' "$PUBLIC_HOST" > "$DIR/.env"
if [ ! -f "$DIR/auth.env" ]; then
  echo "==> Generating password"
  PW="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32)"
  printf 'NEO4J_AUTH=neo4j/%s\n' "$PW" > "$DIR/auth.env"
else
  echo "==> Keeping existing password"
fi
chmod 600 "$DIR/auth.env"

# ── 3. TLS certificate for Bolt (self-signed, 10 years) ─────────────────────
if [ ! -f "$DIR/certs/public.crt" ]; then
  echo "==> Generating self-signed TLS certificate"
  if [[ "$PUBLIC_HOST" =~ ^[0-9.]+$ ]]; then SAN="IP:$PUBLIC_HOST"; else SAN="DNS:$PUBLIC_HOST"; fi
  openssl req -x509 -newkey rsa:4096 -nodes -days 3650 \
    -keyout "$DIR/certs/private.key" -out "$DIR/certs/public.crt" \
    -subj "/CN=$PUBLIC_HOST" -addext "subjectAltName=$SAN,DNS:localhost,IP:127.0.0.1" 2>/dev/null
fi
chown -R "$NEO4J_UID:$NEO4J_UID" "$DIR"/{data,logs,backups,certs}
chmod 600 "$DIR/certs/private.key"

# ── 4. Open Bolt port in the VM firewall (Oracle images REJECT by default) ──
if ! iptables -C INPUT -p tcp --dport 7687 -j ACCEPT 2>/dev/null; then
  echo "==> Opening port 7687 in iptables"
  iptables -I INPUT -p tcp --dport 7687 -j ACCEPT
  if command -v netfilter-persistent >/dev/null; then
    netfilter-persistent save >/dev/null
  else
    echo "   NOTE: netfilter-persistent not found; rule lasts until reboot."
  fi
fi

# ── 5. Start ────────────────────────────────────────────────────────────────
echo "==> Starting Neo4j"
cd "$DIR"
docker compose up -d

echo -n "==> Waiting for Neo4j to start"
for _ in $(seq 1 90); do
  if docker logs neo4j 2>&1 | grep -q 'Started\.'; then echo " ok"; break; fi
  if [ "$(docker inspect -f '{{.State.Status}}' neo4j)" != "running" ]; then
    echo; echo "Neo4j stopped. Last log lines:" >&2; docker logs --tail 40 neo4j >&2; exit 1
  fi
  echo -n "."; sleep 2
done
docker logs neo4j 2>&1 | grep -q 'Started\.' || { echo; echo "Timed out; check: docker logs neo4j" >&2; exit 1; }

# ── 6. Nightly backup at 03:17 (keeps 7 days) ───────────────────────────────
cat > /etc/cron.d/neo4j-backup <<EOF
17 3 * * * root $DIR/backup.sh >> $DIR/logs/backup.log 2>&1
EOF
chmod 644 /etc/cron.d/neo4j-backup

cat <<EOF

Done. Neo4j Community is running.
  Connection URI : bolt+ssc://$PUBLIC_HOST:7687
  Username       : neo4j
  Database       : neo4j
  Password       : sudo cut -d/ -f2 $DIR/auth.env
  Backups        : $DIR/backups (nightly 03:17, 7 kept)

Still needed if not done yet: an Oracle VCN ingress rule for TCP 7687
(Console -> Networking -> VCN -> Security Lists -> Add Ingress Rule).
EOF
