# Self-hosted Neo4j Community (Oracle VM)

Replaces Neo4j Aura Free, which caps the graph at 200k nodes / 400k
relationships (we were at 87% / 96%). Runs on the Oracle Always Free ARM VM
next to Postiz, in its own folder (`/opt/neo4j`) and its own compose project.

| File | What it does |
|---|---|
| `docker-compose.yml` | Neo4j 5.26 Community + APOC, Bolt TLS required, memory capped at 4.5 GB |
| `setup.sh` | One-time install (safe to re-run): swap, password, TLS cert, firewall, start, nightly backup cron |
| `backup.sh` | Nightly offline dump (~1 min downtime), keeps 7; restore steps in its header |
| `../../scripts/copy_aura_to_community.py` | Copies every node, relationship, constraint and index from Aura |

## Memory sizing

| Graph | Data on disk | Heap | Page cache | `mem_limit` |
|---|---|---|---|---|
| Today (~6k properties, 173k nodes) | ~1 GB | 1.5 GB | 1.5 GB | 4.5 GB |
| ~155k properties (~4M nodes) | ~20–25 GB | 2–3 GB | 6–8 GB | 11–12 GB |

The 12 GB VM fits today's graph plus Postiz (~3 GB). For the bigger graph,
resize the VM to 24 GB (Always Free allows 24 GB / 4 OCPU in total), then raise
the three memory lines and `mem_limit` in `docker-compose.yml` and run
`docker compose up -d`.

## Install + migrate

Run on the VM (SSH from Oracle Cloud Shell):

```bash
BR=claude/beautiful-thompson-ougpn2   # or main once merged
RAW=https://raw.githubusercontent.com/aravindpremkumarp/auction-intelligence/$BR
mkdir -p ~/neo4j-setup && cd ~/neo4j-setup
for f in docker-compose.yml setup.sh backup.sh; do curl -fsSLO "$RAW/deploy/neo4j/$f"; done
curl -fsSLO "$RAW/scripts/copy_aura_to_community.py"
sudo bash setup.sh
```

Also add an Oracle **VCN ingress rule**: TCP 7687 from 0.0.0.0/0
(Networking → Virtual Cloud Networks → the VM's subnet → Security List).

Copy the data (runs detached, survives an SSH disconnect):

```bash
cd ~/neo4j-setup
cat > migrate.env <<EOF
SRC_URI=neo4j+s://<aura-id>.databases.neo4j.io
SRC_USER=<aura user>
SRC_PASSWORD=<aura password>
SRC_DATABASE=<aura database>
DST_URI=bolt+ssc://localhost:7687
DST_PASSWORD=$(sudo cut -d/ -f2 /opt/neo4j/auth.env)
EOF
chmod 600 migrate.env
docker run -d --name neo4j-copy --network host --env-file migrate.env \
  -v "$PWD":/w -w /w python:3.12-slim \
  sh -c "pip install -q neo4j && python copy_aura_to_community.py"
docker logs -f neo4j-copy        # ends with "schema done"; look for VERIFY OK
```

Then `rm migrate.env` (it holds the Aura password).

If it stops part-way: re-run the same `docker run` (after `docker rm neo4j-copy`)
— nodes are merged, finished relationship types are skipped. If it stopped
after `cleanup`, use `--step schema` / `--step verify` only.

## Switch the app

New values everywhere `NEO4J_*` is set (Render: both services; GitHub Actions
secrets; local `.env`):

```
NEO4J_URI=bolt+ssc://<vm-public-ip>:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<from /opt/neo4j/auth.env>
NEO4J_DATABASE=neo4j
```

`NEO4J_DATABASE` must be set explicitly: when it is empty the app falls back
to the username. `+ssc` = encrypted, self-signed certificate accepted.

Keep Aura for ~2 weeks as a fallback, then delete it.

## Day-to-day

- Browser UI: `ssh -L 7474:localhost:7474 -L 7687:localhost:7687 ubuntu@<vm>`,
  open http://localhost:7474, connect to `bolt+ssc://localhost:7687`.
- Logs: `docker logs --tail 100 neo4j`; backups: `/opt/neo4j/logs/backup.log`.
- Backups live on the same VM. Copy them off-box (e.g. to R2) for real
  disaster recovery.
