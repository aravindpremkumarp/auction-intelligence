"""
pipeline/embed_descriptions.py
------------------------------
One-shot backfill: embed AuctionProperty.description for every property in
Neo4j, write the vector back as `p.embedding`, and create a vector index.

Run:  python -m pipeline.embed_descriptions

Safe to re-run. Properties that already have an embedding are skipped unless
--force is passed. Batches requests to keep cost and latency reasonable.
"""
from __future__ import annotations

import argparse
import sys
import time

from api.neo4j_client import run_query, session
from pipeline.embeddings import embed_batch, EMBED_DIM

BATCH_SIZE = 64
INDEX_NAME = "property_desc_idx"


def fetch_pending(force: bool) -> list[dict]:
    if force:
        cypher = """
            MATCH (p:AuctionProperty)
            WHERE p.description IS NOT NULL AND trim(p.description) <> ''
            RETURN p.auction_id AS auction_id, p.description AS description
        """
    else:
        cypher = """
            MATCH (p:AuctionProperty)
            WHERE p.description IS NOT NULL AND trim(p.description) <> ''
              AND p.embedding IS NULL
            RETURN p.auction_id AS auction_id, p.description AS description
        """
    return run_query(cypher)


def write_embeddings(rows: list[dict]) -> None:
    cypher = """
        UNWIND $rows AS row
        MATCH (p:AuctionProperty {auction_id: row.auction_id})
        SET p.embedding = row.embedding
    """
    with session() as s:
        s.run(cypher, {"rows": rows})


def ensure_vector_index() -> None:
    cypher = f"""
        CREATE VECTOR INDEX {INDEX_NAME} IF NOT EXISTS
        FOR (p:AuctionProperty) ON (p.embedding)
        OPTIONS {{ indexConfig: {{
            `vector.dimensions`: {EMBED_DIM},
            `vector.similarity_function`: 'cosine'
        }} }}
    """
    with session() as s:
        s.run(cypher)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-embed even if p.embedding exists")
    args = ap.parse_args()

    pending = fetch_pending(args.force)
    total = len(pending)
    if total == 0:
        print("nothing to embed")
        ensure_vector_index()
        return 0

    print(f"embedding {total} properties in batches of {BATCH_SIZE}...")
    done = 0
    for i in range(0, total, BATCH_SIZE):
        batch = pending[i:i + BATCH_SIZE]
        texts = [r["description"] for r in batch]
        vectors = embed_batch(texts)
        payload = [
            {"auction_id": r["auction_id"], "embedding": v}
            for r, v in zip(batch, vectors)
        ]
        write_embeddings(payload)
        done += len(batch)
        print(f"  {done}/{total}")
        time.sleep(0.2)

    print("creating vector index...")
    ensure_vector_index()
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
