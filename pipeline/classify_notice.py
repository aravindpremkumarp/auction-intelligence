"""Classify each :Document as 'single' or 'multi' property notice.

Cluster-count classification:
    Count how many :AuctionProperty rows link to each :Document via
    [:HAS_DOCUMENT]. Set notice_type = 'single' when pc=1 else 'multi',
    and property_count = pc. This is the signal from the original
    scripts/classify_notices.py.

Manual-override guard. Documents flagged with notice_type_overridden=true
are NEVER overwritten — the canonical notice_type stays where the human
put it. Human review (web/review.html classification queue) is the
corrective for cluster-count mistakes, e.g. a multi-lot notice whose
in-scope scrape collapsed to one listing.

Run:  python -m pipeline.classify_notice

Idempotent.
"""
from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from api.neo4j_client import run_query, run_read_query
from pipeline.obs import get_logger

log = get_logger(__name__)


load_dotenv()


def stamp_cluster_counts() -> int:
    """Seed notice_type + property_count from cluster size.

    Skips Documents the human has overridden. Returns the number stamped.
    """
    res = run_query("""
        MATCH (d:Document)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
        WITH d, count(DISTINCT a) AS pc
        WHERE coalesce(d.notice_type_overridden, false) = false
        SET d.property_count = pc,
            d.notice_type = CASE WHEN pc = 1 THEN 'single' ELSE 'multi' END
        RETURN count(d) AS n
    """)
    return int(res[0]["n"]) if res else 0


def print_summary() -> None:
    print()
    print("notice_type distribution:")
    for r in run_read_query("""
      MATCH (d:Document)
      WHERE d.notice_type IS NOT NULL
      RETURN d.notice_type AS notice_type,
             count(*) AS docs,
             sum(d.property_count) AS properties
      ORDER BY notice_type
    """):
        print(f"  {r['notice_type']:<6} docs={r['docs']:>5}  "
              f"properties={r['properties']:>5}")

    print()
    print("Pending classification review:")
    for r in run_read_query("""
      MATCH (d:Document)
      WHERE d.notice_type IS NOT NULL
      RETURN
        sum(CASE WHEN coalesce(d.notice_type_overridden, false) = false
                  AND d.notice_type_verified_at IS NULL THEN 1 ELSE 0 END) AS pending,
        sum(CASE WHEN d.notice_type_verified_at IS NOT NULL THEN 1 ELSE 0 END) AS verified,
        sum(CASE WHEN coalesce(d.notice_type_overridden, false) = true THEN 1 ELSE 0 END) AS overridden
    """):
        print(f"  pending={r['pending']}  verified={r['verified']}  "
              f"overridden={r['overridden']}")


def run() -> int:
    """Public entry point used by pipeline/run_pipeline.py."""
    print("Cluster-count classification")
    tagged = stamp_cluster_counts()
    print(f"  tagged {tagged} Documents (overrides preserved)")
    print_summary()
    return 0


def main() -> int:
    argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    ).parse_args()
    return run()


if __name__ == "__main__":
    sys.exit(main())
