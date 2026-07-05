"""
pipeline/embeddings.py
----------------------
Single embedding backend: Google **gemini-embedding-2** (3072 dims, multimodal).

One model, one vector space. Used by all three Neo4j vector indexes:
  - property_desc_idx     over AuctionProperty.description_embedding (text)
  - notice_markdown_idx   over Document.markdown_embedding          (text)
  - notice_image_idx      over Document.image_embedding             (image / PDF bytes)

Asymmetric retrieval pattern:
  - Documents (text / file bytes) are embedded with **default mode**.
  - Queries are wrapped with the prompt prefix
        "task: search result | query: <text>"
    before embedding. This is Google's documented retrieval pattern that
    keeps query and document embeddings in compatible sub-spaces without
    needing the explicit task_type API on the file path (which only the
    text inputs support).

Requires GOOGLE_API_KEY (or GEMINI_API_KEY) in the environment.
"""
from __future__ import annotations

import os
import time

# ── Gemini multimodal embeddings ────────────────────────────────────────────

GEMINI_EMBED_MODEL = "gemini-embedding-2"
GEMINI_EMBED_DIM = 3072

# Which gateway serves TEXT embeddings: "google" (direct Gemini API, needs
# GOOGLE_API_KEY + its own quota/billing) or "openrouter" (OpenAI-compatible
# /embeddings endpoint on the already-funded OpenRouter key — same underlying
# gemini-embedding-2 model, same vector space). Image embeddings always go
# direct to Google (OpenRouter's embeddings endpoint is text-first).
EMBEDDINGS_PROVIDER = os.getenv("EMBEDDINGS_PROVIDER", "google").strip().lower()
OPENROUTER_EMBED_MODEL = "google/gemini-embedding-2"

# Gemini's input cap for text (8K tokens ≈ 30K chars). Documents longer than
# this are truncated at embed time. For the auction corpus, only a handful of
# multi-page batch notices exceed the cap; the structural / header signal is
# still captured in the first window.
GEMINI_MAX_TEXT_CHARS = 30_000

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
                "Required for Gemini embeddings."
            )
        _genai_client = genai.Client(api_key=api_key)
    return _genai_client


def embed_file_gemini(file_bytes: bytes, mime_type: str) -> list[float]:
    """Embed a file (image jpg/png or PDF) into the gemini-embedding-2 vector
    space. Used for the `notice_image_idx` index."""
    from google.genai import types  # type: ignore

    client = _get_genai_client()
    part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
    resp = client.models.embed_content(
        model=GEMINI_EMBED_MODEL,
        contents=[part],
    )
    return list(resp.embeddings[0].values)


def _embed_text_openrouter(text: str) -> list[float]:
    """Embed text via OpenRouter's OpenAI-compatible /embeddings endpoint,
    routed to the same gemini-embedding-2 model. `dimensions` is pinned to
    GEMINI_EMBED_DIM so vectors stay compatible with the direct-Google ones
    already in the graph."""
    import httpx

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "EMBEDDINGS_PROVIDER=openrouter but OPENROUTER_API_KEY is not set."
        )
    truncated = text[:GEMINI_MAX_TEXT_CHARS]
    resp = httpx.post(
        "https://openrouter.ai/api/v1/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": OPENROUTER_EMBED_MODEL,
            "input": truncated,
            "dimensions": GEMINI_EMBED_DIM,
        },
        timeout=60.0,
    )
    if resp.status_code == 429:
        # Normalize to the quota phrasing embed_descriptions' retry loop
        # already understands.
        raise RuntimeError(f"429 RESOURCE_EXHAUSTED: {resp.text[:200]}")
    resp.raise_for_status()
    return list(resp.json()["data"][0]["embedding"])


def embed_text_gemini(text: str) -> list[float]:
    """Embed a text document (description / markdown) into the
    gemini-embedding-2 vector space, via the configured provider
    (EMBEDDINGS_PROVIDER: "google" direct, or "openrouter" — same model,
    same space). Truncates at GEMINI_MAX_TEXT_CHARS."""
    if EMBEDDINGS_PROVIDER == "openrouter":
        return _embed_text_openrouter(text)
    client = _get_genai_client()
    truncated = text[:GEMINI_MAX_TEXT_CHARS] if len(text) > GEMINI_MAX_TEXT_CHARS else text
    resp = client.models.embed_content(
        model=GEMINI_EMBED_MODEL,
        contents=truncated,
    )
    return list(resp.embeddings[0].values)


def embed_text_batch_gemini(
    texts: list[str],
    sleep_between: float = 0.15,
) -> list[list[float]]:
    """Embed a list of texts sequentially with a gentle rate limit. Returns
    one vector per input. Each call is independent so partial progress is
    not lost on interruption — callers should still write incrementally."""
    out: list[list[float]] = []
    for i, t in enumerate(texts):
        out.append(embed_text_gemini(t))
        if sleep_between and i < len(texts) - 1:
            time.sleep(sleep_between)
    return out


def embed_query_gemini(query_text: str) -> list[float]:
    """Embed a search query for retrieval against any of the three Gemini
    indexes. Wraps the user's text in the asymmetric-retrieval prompt
        "task: search result | query: <text>"
    so the query lives in the right sub-space relative to the raw-mode
    document embeddings.
    """
    formatted = f"task: search result | query: {query_text}"
    if EMBEDDINGS_PROVIDER == "openrouter":
        return _embed_text_openrouter(formatted)
    client = _get_genai_client()
    resp = client.models.embed_content(
        model=GEMINI_EMBED_MODEL,
        contents=formatted,
    )
    return list(resp.embeddings[0].values)
