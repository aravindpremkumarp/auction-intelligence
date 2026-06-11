"""Apply v3 (MinerU+LLM) descriptions from cache to Neo4j with retry-aware writes."""
import json
import pathlib
from api.neo4j_client import run_query, run_read_query


def safe_name(fp: str) -> str:
    return fp.replace('/', '_').replace('\\', '_').replace(':', '_')


cache_dir = pathlib.Path('pipeline/cache/notice_descriptions_v3')

rows = run_read_query("""
  MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document {notice_type: 'single'})
  RETURN a.auction_id AS aid, d.file_path AS fp
""", max_rows=10_000)
print(f"Single-property listing-document pairs: {len(rows)}")

payload = []
for r in rows:
    p = cache_dir / f"{safe_name(r['fp'])}.json"
    if not p.exists():
        continue
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
        desc = d.get('property_description_full')
    except Exception:
        continue
    if isinstance(desc, str) and desc.strip():
        payload.append({'auction_id': r['aid'], 'desc': desc})

print(f"Writable pairs: {len(payload)}")

written = 0
for i in range(0, len(payload), 200):
    batch = payload[i:i + 200]
    for attempt in range(3):
        try:
            run_query("""
                UNWIND $rows AS row
                MATCH (a:AuctionProperty {auction_id: row.auction_id})
                SET a.description = row.desc, a.description_source = 'notice'
            """, {"rows": batch})
            written += len(batch)
            break
        except Exception as e:
            print(f"  retry {attempt + 1}: {type(e).__name__}: {e}")
    else:
        print(f"  GAVE UP on chunk @ {i}")

print(f"\nWrote {written} listings")
print()
for r in run_read_query(
    "MATCH (a:AuctionProperty) RETURN a.description_source AS src, count(*) AS n ORDER BY src"
):
    print(f"  {str(r['src']):<20} {r['n']:>5}")
