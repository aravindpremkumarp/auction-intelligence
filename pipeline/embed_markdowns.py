"""
pipeline/embed_markdowns.py
---------------------------
Embed every cached MinerU markdown into a 3072-dim vector via Google's
gemini-embedding-2 model and write it back as `:Document.markdown_embedding`.
Also (re)creates the `notice_markdown_idx` vector index.

This complements:
  - `pipeline/embed_descriptions.py`  (AuctionProperty.description_embedding)
  - `pipeline/embed_notices.py`       (Document.image_embedding, raw bytes)

The markdown captures the structured *text* of each notice — header, parties,
schedule, terms, table cells reflowed into Markdown. The image_embedding
captures the same notice as a multimodal object (layout, seals, hand stamps,
multi-page structure). Two complementary lenses on the same notice.

Reads markdowns from `pipeline/cache/mineru_markdown/<safe_path>.md`.
Each Document's file_path is normalized to that safe-name to look up its
markdown. Documents without a cached markdown are skipped (and logged).

Run:  python -m pipeline.embed_markdowns

Safe to re-run. Documents that already have a markdown_embedding are skipped
unless --force is passed. Truncates at GEMINI_MAX_TEXT_CHARS.
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from api.neo4j_client import run_read_query, session
from pipeline.embeddings import (
    GEMINI_EMBED_DIM, GEMINI_EMBED_MODEL, GEMINI_MAX_TEXT_CHARS,
    embed_text_gemini,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
MD_DIR = REPO_ROOT / "pipeline" / "cache" / "mineru_markdown"

INDEX_NAME = "notice_markdown_idx"


def safe_name(file_path: str) -> str:
    """Mirror the safe-name used by scripts/ocr_with_mineru.py."""
    return file_path.replace("/", "_").replace("\\", "_").replace(":", "_")


def fetch_pending(force: bool) -> list[dict]:
    if force:
        cypher = """
            MATCH (d:Document)
            WHERE d.file_path IS NOT NULL AND d.file_path <> ''
            RETURN d.file_path AS file_path
        """
    else:
        cypher = """
            MATCH (d:Document)
            WHERE d.file_path IS NOT NULL AND d.file_path <> ''
              AND d.markdown_embedding IS NULL
            RETURN d.file_path AS file_path
        """
    return run_read_query(cypher, max_rows=20_000)


def write_embeddings(rows: list[dict]) -> None:
    cypher = """
        UNWIND $rows AS row
        MATCH (d:Document {file_path: row.file_path})
        SET d.markdown_embedding = row.embedding
    """
    with session() as s:
        s.run(cypher, {"rows": rows})


def ensure_vector_index() -> None:
    cypher = f"""
        CREATE VECTOR INDEX {INDEX_NAME} IF NOT EXISTS
        FOR (d:Document) ON (d.markdown_embedding)
        OPTIONS {{ indexConfig: {{
            `vector.dimensions`: {GEMINI_EMBED_DIM},
            `vector.similarity_function`: 'cosine'
        }} }}
    """
    with session() as s:
        s.run(cypher)


def _embed_one(file_path: str) -> dict:
    """Worker: load markdown from disk, embed, return result dict.

    Returns one of:
      {"file_path": fp, "embedding": [...]}   on success
      {"file_path": fp, "missing": True}      if md not on disk or empty
      {"file_path": fp, "quota": True}        on daily/free-tier quota
      {"file_path": fp, "error": "..."}       on other failures
    """
    md_path = MD_DIR / f"{safe_name(file_path)}.md"
    if not md_path.exists():
        return {"file_path": file_path, "missing": True}
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"file_path": file_path, "error": f"read-fail: {e}"}
    if not text.strip():
        return {"file_path": file_path, "missing": True}

    for attempt in range(2):
        try:
            vec = embed_text_gemini(text)
            return {"file_path": file_path, "embedding": vec}
        except Exception as e:
            msg = str(e)
            if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
                if "PerDay" in msg or "free_tier" in msg or attempt == 1:
                    return {"file_path": file_path, "quota": True}
                time.sleep(30)
                continue
            return {"file_path": file_path, "error": f"embed-fail: {e}"}
    return {"file_path": file_path, "error": "embed-fail: exhausted retries"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="re-embed even if d.markdown_embedding exists")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N Documents (staged rollout)")
    ap.add_argument("--workers", type=int, default=8,
                    help="thread-pool size for concurrent Gemini calls")
    ap.add_argument("--write-batch", type=int, default=64,
                    help="rows per Neo4j UNWIND write")
    args = ap.parse_args()

    pending = fetch_pending(args.force)
    if args.limit:
        pending = pending[:args.limit]
    total = len(pending)

    if total == 0:
        print("nothing to embed")
        ensure_vector_index()
        return 0

    print(f"Embedding {total} markdowns via {GEMINI_EMBED_MODEL} "
          f"({GEMINI_EMBED_DIM} dims, max {GEMINI_MAX_TEXT_CHARS:,} chars) "
          f"with {args.workers} workers")
    print(f"Reading from {MD_DIR}")
    print("(re-runs skip Documents that already have d.markdown_embedding; "
          "pass --force to override)")

    payloads: list[dict] = []
    done = 0
    failed = 0
    missing_md = 0
    quota_exhausted = False

    def safe_write(rows: list[dict]) -> bool:
        for attempt in range(2):
            try:
                write_embeddings(rows)
                return True
            except Exception as e:
                if attempt == 0:
                    print(f"  [neo4j retry] {type(e).__name__}: {e}")
                    time.sleep(2)
                else:
                    print(f"  [neo4j FAIL] giving up on this batch: {e}")
                    return False
        return False

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_embed_one, r["file_path"]): r["file_path"]
                   for r in pending}
        try:
            for fut in as_completed(futures):
                res = fut.result()
                if res.get("quota"):
                    print("  [QUOTA EXHAUSTED] stopping cleanly.")
                    quota_exhausted = True
                    break
                if res.get("missing"):
                    missing_md += 1
                    continue
                if "error" in res:
                    print(f"  [{res['error']}] {res['file_path']}")
                    failed += 1
                    continue
                payloads.append({"file_path": res["file_path"],
                                 "embedding": res["embedding"]})
                done += 1

                if len(payloads) >= args.write_batch:
                    if safe_write(payloads):
                        payloads = []

                if done % 50 == 0 or done == total:
                    elapsed = time.time() - t0
                    rate = done / elapsed if elapsed > 0 else 0
                    eta = (total - done - missing_md - failed) / rate if rate > 0 else 0
                    print(f"  [{done}/{total}]  rate={rate:.1f}/s  eta={eta:.0f}s  "
                          f"(failed={failed}, missing_md={missing_md})", flush=True)
        finally:
            if quota_exhausted:
                # Cancel pending futures so the executor exits promptly.
                for f in futures:
                    f.cancel()

    if payloads:
        safe_write(payloads)

    print(f"\nWrote {done} embeddings  failed={failed}  "
          f"missing_md={missing_md}  quota_exhausted={quota_exhausted}")
    print("Creating vector index (idempotent)...")
    ensure_vector_index()
    if quota_exhausted:
        print("\nNOTE: Daily quota exhausted before completion. Re-run after "
              "reset — already-embedded Documents are skipped automatically.")
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
