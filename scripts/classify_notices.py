"""Tag :Document nodes with property_count + notice_type ('single' | 'multi').

A sales notice can cover one or many properties. Listings sharing a notice
already cluster around the same :Document via [:HAS_DOCUMENT] (see
load_tn_to_neo4j.py). This script counts the cluster size and stamps each
Document with property_count + notice_type so downstream extractors can route
notices to the single- vs multi-property pipeline.

**Manual-override guard.** Documents flagged with ``notice_type_overridden = true``
are NEVER overwritten by this script — their notice_type was decided by manual
review (see scripts/_apply_review_corrections.py and
scripts/_apply_multi_to_single_overrides.py).

Run:  python -m scripts.classify_notices

Idempotent. Safe to re-run on every ingest — only un-overridden Documents
are touched, so manual review work survives forever.
"""
from __future__ import annotations

from api.neo4j_client import run_query, run_read_query


def main() -> int:
    print("Classifying Documents by property_count...")

    res = run_query("""
        MATCH (d:Document)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
        WITH d, count(DISTINCT a) AS pc
        WHERE coalesce(d.notice_type_overridden, false) = false
        SET d.property_count = pc,
            d.notice_type = CASE WHEN pc = 1 THEN 'single' ELSE 'multi' END
        RETURN count(d) AS documents_tagged
    """)
    tagged = res[0]["documents_tagged"] if res else 0
    print(f"  Tagged {tagged} Documents (overrides preserved)")

    overridden = run_read_query("""
        MATCH (d:Document)
        WHERE d.notice_type_overridden = true
        RETURN count(d) AS overridden
    """)
    n_over = overridden[0]["overridden"] if overridden else 0
    print(f"  Manual overrides preserved: {n_over}")

    print()
    print("notice_type distribution:")
    rows = run_read_query("""
        MATCH (d:Document)
        WHERE d.notice_type IS NOT NULL
        RETURN d.notice_type AS notice_type,
               count(*) AS documents,
               sum(d.property_count) AS properties_covered
        ORDER BY notice_type
    """)
    for r in rows:
        print(f"  {r['notice_type']:<6} "
              f"documents={r['documents']:>5}  "
              f"properties_covered={r['properties_covered']:>5}")

    print()
    print("Top 20 multi-property notices (by listing count):")
    multis = run_read_query("""
        MATCH (d:Document)
        WHERE d.property_count >= 2
        RETURN d.file_path AS notice, d.property_count AS pc
        ORDER BY pc DESC
        LIMIT 20
    """)
    for r in multis:
        print(f"  pc={r['pc']:>3}  {r['notice']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
