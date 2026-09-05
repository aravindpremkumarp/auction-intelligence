"""
pipeline/run_pipeline.py
------------------------
Orchestrator: runs the graph-side pipeline stages sequentially.

Inputs it expects to already exist on the graph:
  * :AuctionProperty + :Document nodes (scripts/load_tn_to_neo4j,
    scripts/upload_downloads_to_r2)
  * Document.markdown (scripts/ocr_with_mineru / scripts/ocr_missing_markdowns)
  * Document.extraction_json (pipeline/load_extractions — the LangExtract
    grounded extraction, run from the review UI or by hand)

Stages, in order:
  1.3  classify_notice      cluster-count single/multi tag (human review corrects)
  4.4  promote_extractions  grounded extractions -> :Lot / :Parcel spine
  4.5  apply_extractions    grounded per-lot values -> :AuctionProperty
  5    link_reauctions      :SAME_PROPERTY_AS across re-listings
  6    schema cache         refresh the :SchemaCache node for /chat

The legacy "Path A" (flat vision-LLM blob -> verify_and_enrich -> load_enriched)
was retired; the grounded LangExtract path is the only extractor.

Usage:
  python -m pipeline.run_pipeline                  # Full run
  python -m pipeline.run_pipeline --pilot          # First PILOT_SIZE records only
  python -m pipeline.run_pipeline --limit 50       # First 50 records (overrides --pilot)
  python -m pipeline.run_pipeline --skip-classify  # Skip the notice-classification stage
"""

import argparse
import time

from pipeline.config import PILOT_SIZE


def main():
    parser = argparse.ArgumentParser(description="Bank Auction Intelligence Pipeline")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit number of records (overrides --pilot)")
    parser.add_argument("--pilot", action="store_true",
                        help=f"Process first PILOT_SIZE ({PILOT_SIZE}) records only")
    parser.add_argument("--skip-classify", action="store_true",
                        help="Skip the notice-classification stage (1.3)")
    args = parser.parse_args()

    effective_limit = args.limit if args.limit is not None else (PILOT_SIZE if args.pilot else None)

    t_start = time.time()

    # Stage 1.3: classify notices (cluster count; human review corrects).
    # Runs against the :Document nodes and seeds the review UI's
    # classification queue; a reviewer override is never overwritten.
    if not args.skip_classify:
        print("\n" + "="*60)
        print("STAGE 1.3: Classify notices (single / multi)")
        print("="*60)
        from pipeline.classify_notice import run as run_classify
        run_classify()
    else:
        print("\n[SKIPPED] Stage 1.3: Classify notices")

    # Stage 4.4: resolve the grounded extractions into the graph — one :Lot
    # per property with its description, extents, identifiers and place,
    # then the derived :Parcel layer. This is the step that puts the
    # extracted entities INTO the graph.
    #
    # It must run BEFORE 4.5: apply_extractions' area comparer reads each
    # lot's headline extent off the graph, so running the two the other
    # way round judges the listings against stale sizes.
    #
    # Idempotent (MERGEs on stable keys), never writes :AuctionProperty.
    # With --limit only part of the corpus is promoted, and the parcel
    # phase needs the whole corpus to group correctly, so it is skipped on
    # a limited run rather than left with a partial, misleading grouping.
    print("\n" + "="*60)
    print("STAGE 4.4: Promote grounded extractions into :Lot / :Parcel")
    print("="*60)
    from pipeline.promote_extractions import run as run_promote_extractions
    run_promote_extractions(limit=effective_limit, filename=None,
                            dry_run=False,
                            skip_parcels=effective_limit is not None)

    # Stage 4.5: grounded extractions (Document.extraction_json, written by
    # pipeline/load_extractions.py) are applied to every AuctionProperty
    # whose lot they match — fields, description, agreement verdicts.
    print("\n" + "="*60)
    print("STAGE 4.5: Apply grounded extractions to AuctionProperty")
    print("="*60)
    from pipeline.apply_extractions import run as run_apply_extractions
    run_apply_extractions(limit=effective_limit)

    print("\n" + "="*60)
    print("STAGE 5: Link Re-auctioned Properties (:SAME_PROPERTY_AS)")
    print("="*60)
    from scripts.link_reauctions import run as link_reauctions
    link_reauctions()

    # Stage 6: Refresh the durable schema cache (:SchemaCache node) so /chat's
    # describe_schema reads it in one query instead of re-running ~25 live
    # introspection queries on a cold start. Best-effort — a refresh failure
    # must not fail the pipeline (the API falls back to a live compute).
    print("\n" + "="*60)
    print("STAGE 6: Refresh schema cache (:SchemaCache node)")
    print("="*60)
    try:
        from api.tools.cypher_tools import describe_schema
        describe_schema(refresh=True)
        print("  schema cache refreshed")
    except Exception as e:
        print(f"  WARNING: schema cache refresh failed ({type(e).__name__}: {e})")

    elapsed = time.time() - t_start
    print(f"\n{'='*60}")
    print(f"PIPELINE COMPLETE — Total time: {elapsed:.1f}s")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
