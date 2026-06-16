"""
pipeline/run_pipeline.py
------------------------
Orchestrator: runs all pipeline stages sequentially.

Usage:
  python -m pipeline.run_pipeline                  # Full run
  python -m pipeline.run_pipeline --pilot          # First PILOT_SIZE records only
  python -m pipeline.run_pipeline --limit 50       # First 50 records (overrides --pilot)
  python -m pipeline.run_pipeline --skip-ocr       # Skip Stage 1 (use existing cache)
  python -m pipeline.run_pipeline --skip-descriptions  # Skip classify/extract/apply description stages
  python -m pipeline.run_pipeline --verify-only    # Only run Stage 1.5 + Stage 4 (verified path)
  python -m pipeline.run_pipeline --legacy         # Run old Stage 2/3/4 path instead of verify path
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
    parser.add_argument("--skip-ocr", action="store_true",
                        help="Skip OCR extraction (reuse existing cache)")
    parser.add_argument("--skip-descriptions", action="store_true",
                        help="Skip classify/extract/apply description stages")
    parser.add_argument("--verify-only", action="store_true",
                        help="Only run verify + load-verified (no OCR, no legacy stages)")
    parser.add_argument("--legacy", action="store_true",
                        help="Run old lexical-graph → normalize → load path (instead of verify path)")
    args = parser.parse_args()

    effective_limit = args.limit if args.limit is not None else (PILOT_SIZE if args.pilot else None)

    t_start = time.time()

    # Stage 1: OCR + Entity Extraction (vision LLM per file, cached)
    if not args.skip_ocr and not args.verify_only:
        print("\n" + "="*60)
        print("STAGE 1: OCR + Entity Extraction")
        print("="*60)
        import asyncio
        from pipeline.ocr_extract import run_extraction
        asyncio.run(run_extraction(limit=effective_limit))
    else:
        print("\n[SKIPPED] Stage 1: OCR Extraction")

    # Stages 1.3/1.4/1.45: classify -> extract per-type -> apply (description pipeline)
    # These run against the Neo4j :Document nodes (which the verify+load stages
    # populate), so they make sense as a post-load step in re-runs, AND as a
    # pre-verify step on first run. We place them here so they can be skipped
    # independently and so the verify-only path also picks them up.
    if not args.skip_descriptions:
        print("\n" + "="*60)
        print("STAGE 1.3: Classify notices (single / multi)")
        print("="*60)
        from pipeline.classify_notice import run as run_classify
        run_classify(limit=effective_limit)

        print("\n" + "="*60)
        print("STAGE 1.4: Extract per-property descriptions")
        print("="*60)
        from pipeline.extract_descriptions import run as run_extract_descs
        run_extract_descs(limit=effective_limit)

        print("\n" + "="*60)
        print("STAGE 1.45: Apply descriptions to AuctionProperty")
        print("="*60)
        from pipeline.apply_descriptions import run as run_apply_descs
        run_apply_descs()
    else:
        print("\n[SKIPPED] Stages 1.3/1.4/1.45: Description pipeline")

    if args.legacy:
        # Legacy path: build lexical graph, normalize, load flat enrichment.
        print("\n" + "="*60)
        print("STAGE 2: Lexical Graph Construction (legacy)")
        print("="*60)
        from pipeline.lexical_graph import build_lexical_graph
        build_lexical_graph()

        print("\n" + "="*60)
        print("STAGE 3: Entity Normalization (legacy)")
        print("="*60)
        from pipeline.normalize import normalize_entities
        normalize_entities()

        print("\n" + "="*60)
        print("STAGE 4: Load Enriched Data to Neo4j (legacy)")
        print("="*60)
        from pipeline.load_enriched import load_to_neo4j
        load_to_neo4j()
    else:
        # Verify path: compare scraped vs PDF, merge extras, load + Document nodes.
        print("\n" + "="*60)
        print("STAGE 1.5: Verify + Enrich (PDF is source of truth)")
        print("="*60)
        from pipeline.verify_and_enrich import run as run_verify
        run_verify(limit=effective_limit, pilot=False)  # limit already resolved

        print("\n" + "="*60)
        print("STAGE 4: Load Verified + Enriched to Neo4j")
        print("="*60)
        from pipeline.load_enriched import load_verified_enriched
        load_verified_enriched()

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
