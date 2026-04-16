"""
pipeline/embeddings.py
----------------------
Shared embedding helper used by the backfill script and the semantic search
tool. Uses OpenAI `text-embedding-3-small` (1536 dims) via the openai SDK.

Requires OPENAI_API_KEY in the environment. Kept deliberately small so both
the pipeline and the API layer can depend on it without a framework.
"""
from __future__ import annotations

import os
from openai import OpenAI

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Semantic search requires a direct "
                "OpenAI key (OpenRouter does not reliably proxy embeddings)."
            )
        _client = OpenAI(api_key=api_key)
    return _client


def embed_text(text: str) -> list[float]:
    resp = get_client().embeddings.create(model=EMBED_MODEL, input=text)
    return resp.data[0].embedding


def embed_batch(texts: list[str]) -> list[list[float]]:
    resp = get_client().embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]
