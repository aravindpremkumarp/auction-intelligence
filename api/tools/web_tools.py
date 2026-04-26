"""
api/tools/web_tools.py
----------------------
Tavily-backed web search tool exposed to the agent.

Returns a normalized {sources, query} envelope (or {error}) so the agent can
weave web information into its prose answer with bracketed citations and the
frontend can render Perplexity-style source chips under the AI bubble.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from pipeline.config import TAVILY_API_KEY

try:
    from tavily import TavilyClient
except ImportError:  # SDK is optional at import time; tool degrades to {error}.
    TavilyClient = None  # type: ignore[assignment]


_TIMEOUT_S = 10
_SNIPPET_CAP = 500
_MAX_RESULTS_CAP = 5
_CACHE_CAP = 500

_client_singleton: object | None = None
_client_initialized = False
_cache: dict[tuple[str, int], dict] = {}


def _client():
    global _client_singleton, _client_initialized
    if _client_initialized:
        return _client_singleton
    _client_initialized = True
    if not TAVILY_API_KEY or TavilyClient is None:
        _client_singleton = None
    else:
        _client_singleton = TavilyClient(api_key=TAVILY_API_KEY)
    return _client_singleton


def _normalize(raw: dict, query: str) -> dict:
    sources = []
    for r in raw.get("results", []) or []:
        url = r.get("url") or ""
        if not url:
            continue
        domain = urlparse(url).netloc
        if domain.startswith("www."):
            domain = domain[4:]
        content = r.get("content") or ""
        sources.append({
            "title": r.get("title") or url,
            "url": url,
            "snippet": content[:_SNIPPET_CAP],
            "domain": domain,
            "score": r.get("score"),
        })
    return {"sources": sources, "query": query}


async def internet_search(query: str, max_results: int = 5) -> dict:
    """
    Returns:
      {"sources": [...], "query": str} on success or empty results
      {"error": "<reason>"} on failure or when disabled
    """
    q = (query or "").strip()
    if not q:
        return {"error": "Empty query."}

    client = _client()
    if client is None:
        return {"error": "Web search not configured."}

    n = max(1, min(_MAX_RESULTS_CAP, int(max_results or 5)))
    cache_key = (q.lower(), n)
    if cache_key in _cache:
        return _cache[cache_key]

    def _do_search():
        return client.search(
            query=q,
            search_depth="basic",
            max_results=n,
            include_answer=False,
            include_raw_content=False,
            include_images=False,
        )

    try:
        raw = await asyncio.wait_for(asyncio.to_thread(_do_search), timeout=_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"error": "Web search timed out."}
    except Exception as e:
        msg = str(e).lower()
        if "429" in msg or "rate" in msg:
            return {"error": "Web search rate-limited; try again later."}
        return {"error": f"Web search failed: {type(e).__name__}"}

    if not isinstance(raw, dict):
        return {"error": "Web search returned an unexpected response."}

    out = _normalize(raw, q)

    if len(_cache) > _CACHE_CAP:
        _cache.clear()
    _cache[cache_key] = out
    return out
