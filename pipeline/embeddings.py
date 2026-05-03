"""
pipeline/embeddings.py
----------------------
Two embedding backends:

1. **OpenAI text-embedding-3-small** (1536 dims) — used by `property_desc_idx`
   for narrow property-description semantic search. Requires OPENAI_API_KEY.

2. **Gemini gemini-embedding-2** (3072 dims, multimodal) — used by
   `notice_image_idx` for full-notice semantic search. Embeds image/PDF bytes
   directly via Google AI Studio. Requires GOOGLE_API_KEY.

Kept deliberately small so the pipeline and the API layer can depend on it
without a framework.
"""
from __future__ import annotations

import os
from openai import OpenAI

# ── OpenAI text embeddings (legacy property_desc_idx) ────────────────────────

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536

_openai_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Semantic search requires a direct "
                "OpenAI key (OpenRouter does not reliably proxy embeddings)."
            )
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def embed_text(text: str) -> list[float]:
    resp = get_client().embeddings.create(model=EMBED_MODEL, input=text)
    return resp.data[0].embedding


def embed_batch(texts: list[str]) -> list[list[float]]:
    resp = get_client().embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]


# ── Gemini multimodal embeddings (new notice_image_idx) ──────────────────────

GEMINI_EMBED_MODEL = "gemini-embedding-2"
GEMINI_EMBED_DIM = 3072

_genai_client = None


def _get_genai_client():
    """Lazy-init the google-genai client. Reads GOOGLE_API_KEY (or
    GEMINI_API_KEY) from the environment."""
    global _genai_client
    if _genai_client is None:
        from google import genai  # type: ignore

        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GOOGLE_API_KEY (or GEMINI_API_KEY) is not set. "
                "Required for Gemini multimodal embeddings."
            )
        _genai_client = genai.Client(api_key=api_key)
    return _genai_client


def embed_file_gemini(file_bytes: bytes, mime_type: str) -> list[float]:
    """Embed a file (image jpg/png or PDF) into the gemini-embedding-2 vector
    space (3072 dims by default). Used for the `notice_image_idx` index."""
    from google.genai import types  # type: ignore

    client = _get_genai_client()
    part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
    resp = client.models.embed_content(
        model=GEMINI_EMBED_MODEL,
        contents=[part],
    )
    return list(resp.embeddings[0].values)


def embed_query_gemini(query_text: str) -> list[float]:
    """Embed a search query for retrieval against `notice_image_idx`. Wraps
    the user's text in Google's recommended asymmetric-retrieval prompt:
        "task: search result | query: <text>"
    so the query and document embeddings live in compatible sub-spaces.
    """
    client = _get_genai_client()
    formatted = f"task: search result | query: {query_text}"
    resp = client.models.embed_content(
        model=GEMINI_EMBED_MODEL,
        contents=formatted,
    )
    return list(resp.embeddings[0].values)
