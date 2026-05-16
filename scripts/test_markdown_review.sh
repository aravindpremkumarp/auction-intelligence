#!/usr/bin/env bash
# scripts/test_markdown_review.sh
# ---------------------------------
# Smoke-tests the markdown-quality review backend end-to-end against the
# live Neo4j project. Runs tests 1, 2, and 5 from PR #112's test plan
# (helper-extraction regression, scoring backfill, on-insert hook).
#
# Tests 3, 4, 6 (API + browser UI) are interactive and not covered here.
#
# Usage: ./scripts/test_markdown_review.sh
set -uo pipefail

cd "$(dirname "$0")/.."

PASS=0
FAIL=0

step() { echo; echo "=== $1 ==="; }
ok()   { echo "  PASS — $1"; PASS=$((PASS + 1)); }
bad()  { echo "  FAIL — $1"; FAIL=$((FAIL + 1)); }

# ── Test 1: helper extraction is non-breaking ───────────────────────────────
step "1. list_notice_queue still returns notice-sorted properties"
OUT=$(python - <<'PY' 2>&1
from api.review.queries import list_notice_queue
r = list_notice_queue(size=3)
print(f"TOTAL={r['total']}")
print(f"ROWS={len(r['rows'])}")
for row in r['rows']:
    print(f"  {row.get('filename')}  props={len(row.get('properties') or [])}")
PY
)
echo "$OUT"
if echo "$OUT" | grep -qE '^ROWS=[1-9]'; then
    ok "list_notice_queue returned rows"
else
    bad "list_notice_queue returned no rows (or errored)"
fi

# ── Test 2: scoring backfill ────────────────────────────────────────────────
step "2. score_markdown --limit 50"
if python -m pipeline.score_markdown --limit 50; then
    ok "scorer ran without error"
else
    bad "scorer exited non-zero"
fi

step "2b. spot-check the bottom 10 lowest-scoring Documents"
python - <<'PY'
from api.neo4j_client import run_read_query
rows = run_read_query("""
    MATCH (d:Document)
    WHERE d.markdown_quality_score IS NOT NULL
    RETURN d.filename                  AS filename,
           d.markdown_quality_score    AS score,
           coalesce(d.property_count, 0) AS prop_count,
           size(d.markdown)            AS md_len
    ORDER BY d.markdown_quality_score ASC, d.filename ASC
    LIMIT 10
""", max_rows=10)
if not rows:
    print("  (no scored Documents found — did the scorer write anything?)")
else:
    print(f"  {'score':>6}  {'props':>5}  {'md_len':>8}  filename")
    for r in rows:
        print(f"  {r['score']:>6}  {r['prop_count']:>5}  {r['md_len']:>8}  {r['filename']}")
PY

step "2c. score distribution"
python - <<'PY'
from api.neo4j_client import run_read_query
r = run_read_query("""
    MATCH (d:Document) WHERE d.markdown IS NOT NULL AND d.markdown <> ''
    RETURN
      sum(CASE WHEN d.markdown_quality_score IS NULL THEN 1 ELSE 0 END)             AS unscored,
      sum(CASE WHEN d.markdown_quality_score <  50  THEN 1 ELSE 0 END)              AS lt50,
      sum(CASE WHEN d.markdown_quality_score >= 50 AND d.markdown_quality_score < 80 THEN 1 ELSE 0 END) AS mid,
      sum(CASE WHEN d.markdown_quality_score >= 80 THEN 1 ELSE 0 END)               AS ge80,
      count(*) AS total
""", max_rows=1)
if r:
    d = r[0]
    print(f"  total={d['total']}  unscored={d['unscored']}  <50={d['lt50']}  "
          f"50-79={d['mid']}  >=80={d['ge80']}")
PY

# ── Test 5: on-insert hook ──────────────────────────────────────────────────
step "5. load_markdowns_to_neo4j --limit 1 scores inline"
BEFORE=$(python - <<'PY'
from api.neo4j_client import run_read_query
r = run_read_query("""
    MATCH (d:Document)
    WHERE d.markdown_quality_scored_at IS NOT NULL
    RETURN max(d.markdown_quality_scored_at) AS t
""", max_rows=1)
print(str((r[0]['t'] if r and r[0] else '')) or 'NONE')
PY
)
echo "  latest scored_at before load: $BEFORE"

# Force the loader so it writes (and triggers score_freshly_loaded) even for
# Documents that already have markdown — otherwise the hook does nothing on
# a clean DB.
python -m pipeline.load_markdowns_to_neo4j --limit 1 --force || true

AFTER=$(python - <<'PY'
from api.neo4j_client import run_read_query
r = run_read_query("""
    MATCH (d:Document)
    WHERE d.markdown_quality_scored_at IS NOT NULL
    RETURN max(d.markdown_quality_scored_at) AS t
""", max_rows=1)
print(str((r[0]['t'] if r and r[0] else '')) or 'NONE')
PY
)
echo "  latest scored_at after  load: $AFTER"

if [ "$AFTER" != "$BEFORE" ] && [ "$AFTER" != "NONE" ]; then
    ok "scored_at advanced — on-insert hook ran"
else
    bad "scored_at did not advance — hook may not be wired"
fi

# ── Summary ─────────────────────────────────────────────────────────────────
echo
echo "================================="
echo " PASS: $PASS    FAIL: $FAIL"
echo "================================="
[ "$FAIL" -eq 0 ]
