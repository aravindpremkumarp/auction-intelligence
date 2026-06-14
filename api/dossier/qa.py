"""
api/dossier/qa.py
-----------------
Retrieval core for dossier Q&A. The chat agent's ``query_user_dossier`` tool
calls :func:`answer_from_dossier`, which pulls the caller's OCR'd documents and
returns grounded **excerpts** — the agent then composes the answer from them
(same "ground every answer in tool output" contract as the graph tools).

Why excerpts, not the whole text: a scanned 15-page deed is large; returning
keyword-windowed excerpts keeps the tool payload bounded and points the model at
the relevant passages. Retrieval is deliberately dependency-free (keyword
windowing, no embeddings) so this slice ships without new infra; a semantic
upgrade can swap out :func:`extract_excerpt` later.
"""
from __future__ import annotations

import re

from api.dossier import repository as repo

# Tiny stopword set so query terms like "what does my EC say about mortgages"
# reduce to the load-bearing words ("ec", "mortgages").
_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "of", "in", "on", "for", "to", "and", "or", "my",
    "me", "i", "we", "our", "you", "your", "what", "which", "who", "whom",
    "this", "that", "these", "those", "it", "its", "about", "with", "from",
    "as", "at", "by", "any", "all", "say", "says", "said", "tell", "show",
    "have", "has", "had", "there", "their", "can", "could", "would", "should",
    "please", "give", "list", "find", "does", "doc", "document", "documents",
    "if", "so", "no", "up", "vs", "am", "us",
}

_WORD_RE = re.compile(r"[a-z0-9]+")

# Bounds on what the tool returns to the model.
DEFAULT_PER_DOC_CHARS = 1500
DEFAULT_MAX_DOCS = 8
_WINDOW = 240  # chars of context on each side of a keyword hit


def query_terms(query: str) -> list[str]:
    """Load-bearing search terms from a natural-language question."""
    # Keep 2-char tokens so meaningful acronyms (EC) survive; most 2-letter
    # English words are stopwords and filtered above.
    terms = [w for w in _WORD_RE.findall((query or "").lower())
             if len(w) >= 2 and w not in _STOPWORDS]
    # De-dupe, preserve order.
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def extract_excerpt(
    text: str, query: str, *, per_doc_chars: int = DEFAULT_PER_DOC_CHARS,
) -> str:
    """Keyword-windowed excerpt of ``text`` for ``query``.

    Builds windows around each query-term hit (case-insensitive), merges
    overlaps, and concatenates with ``…`` separators up to ``per_doc_chars``.
    When no term matches (or the query is empty), returns the document head so
    the model still has grounding to work with.
    """
    text = text or ""
    if not text:
        return ""
    terms = query_terms(query)
    if not terms:
        return text[:per_doc_chars]

    low = text.lower()
    spans: list[tuple[int, int]] = []
    for t in terms:
        start = 0
        while True:
            i = low.find(t, start)
            if i == -1:
                break
            spans.append((max(0, i - _WINDOW), min(len(text), i + len(t) + _WINDOW)))
            start = i + len(t)
    if not spans:
        return text[:per_doc_chars]

    pieces: list[str] = []
    total = 0
    for s, e in _merge_spans(spans):
        chunk = text[s:e].strip()
        if not chunk:
            continue
        prefix = "… " if s > 0 else ""
        suffix = " …" if e < len(text) else ""
        piece = f"{prefix}{chunk}{suffix}"
        if total + len(piece) > per_doc_chars:
            piece = piece[: max(0, per_doc_chars - total)]
            pieces.append(piece)
            break
        pieces.append(piece)
        total += len(piece)
    return "\n".join(pieces)


def build_matches(
    docs: list[dict], query: str, *,
    max_docs: int = DEFAULT_MAX_DOCS, per_doc_chars: int = DEFAULT_PER_DOC_CHARS,
) -> list[dict]:
    """Turn raw document rows into bounded, grounded match objects.

    Docs with a keyword hit rank ahead of those without; ties keep input
    (newest-first) order. At most ``max_docs`` are returned.
    """
    terms = query_terms(query)
    scored: list[tuple[int, int, dict]] = []
    for idx, d in enumerate(docs):
        text = d.get("ocr_text") or ""
        low = text.lower()
        hits = sum(low.count(t) for t in terms) if terms else 0
        excerpt = extract_excerpt(text, query, per_doc_chars=per_doc_chars)
        scored.append((
            0 if hits > 0 else 1,  # hits first
            idx,                    # stable tiebreak
            {
                "dossier_id": d.get("dossier_id"),
                "dossier_title": d.get("dossier_title"),
                "property_label": d.get("property_label"),
                "doc_id": d.get("doc_id"),
                "filename": d.get("filename"),
                "doc_type": d.get("doc_type"),
                "category": d.get("category"),
                "excerpt": excerpt,
            },
        ))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [m for _, _, m in scored[:max_docs]]


async def answer_from_dossier(
    supabase_id: str | None, query: str, *,
    dossier_id: str | None = None, doc_type: str | None = None,
) -> dict:
    """Retrieve grounded excerpts from the caller's dossier documents.

    Statuses:
      * ``login_required`` — no authenticated user.
      * ``no_documents``   — the user has no OCR'd documents in scope.
      * ``ok``             — ``matches`` holds grounded excerpts to answer from.
    """
    if not supabase_id:
        return {"status": "login_required", "matches": [], "count": 0}
    docs = await repo.search_user_documents(
        supabase_id, dossier_id=dossier_id, doc_type=doc_type,
    )
    if not docs:
        return {
            "status": "no_documents", "matches": [], "count": 0,
            "note": "No processed documents found in the user's dossier for "
                    "this scope. They may need to upload documents first.",
        }
    matches = build_matches(docs, query)
    return {"status": "ok", "query": query, "matches": matches, "count": len(matches)}
