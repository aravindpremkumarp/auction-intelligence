#!/usr/bin/env bash
# Nightly offline dump of the Neo4j Community database.
#
# Community can only dump a stopped database, so Neo4j is down for about a
# minute. Run from cron at a quiet hour (setup.sh installs 03:17 daily).
# Keeps the newest KEEP dumps in /opt/neo4j/backups.
#
# Restore (overwrites the live graph):
#   cd /opt/neo4j && docker compose stop neo4j
#   docker compose run --rm --no-deps -e NEO4J_PLUGINS='[]' neo4j \
#     neo4j-admin database load neo4j --from-path=/backups/restore --overwrite-destination=true
#   (first: mkdir -p backups/restore && cp backups/neo4j-<date>.dump backups/restore/neo4j.dump
#    && chown -R 7474:7474 backups)
#   docker compose start neo4j
set -euo pipefail

DIR=/opt/neo4j
KEEP=7
STAMP="$(date +%Y%m%d-%H%M)"
cd "$DIR"

echo "[$(date -Is)] backup start"
docker compose stop neo4j
# Always bring Neo4j back, even if the dump fails.
trap 'docker compose start neo4j >/dev/null; echo "[$(date -Is)] neo4j restarted"' EXIT

docker compose run --rm --no-deps -e NEO4J_PLUGINS='[]' neo4j \
  neo4j-admin database dump neo4j --to-path=/backups --overwrite-destination=true

mv "backups/neo4j.dump" "backups/neo4j-$STAMP.dump"
ls -1t backups/neo4j-*.dump | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "[$(date -Is)] backup ok: backups/neo4j-$STAMP.dump ($(du -h "backups/neo4j-$STAMP.dump" | cut -f1))"
