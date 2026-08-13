"""
pipeline/classify_document.py
-----------------------------
Doc-type classifier for the dossier locker: given the OCR'd text of one
uploaded user document, place it into the 9-category / ~50-type taxonomy
(``api/dossier/taxonomy.py``).

Modeled on ``pipeline/classify_notice.py``'s LLM call (OpenRouter,
aiohttp, JSON verdict), but:
  * the label set is the large dossier taxonomy, injected into the prompt from
    the single source of truth so it never drifts, and
  * it runs at request time on ONE document (no bulk batching / Neo4j writes —
    the dossier repository persists the verdict), so this module just exposes
    a single ``classify_document_text`` coroutine.

``classify_notice`` is NOT reusable here: it's a binary single-vs-multi notice
classifier with no doc-type taxonomy scaffolding (design doc, "Classify").
"""
from __future__ import annotations

import asyncio
import json

import aiohttp
from dotenv import load_dotenv

from api.dossier import taxonomy as tax
from pipeline.config import (
    MAX_RETRIES,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL_DOC_CLASSIFY,
    PROMPTS_DIR,
)
from pipeline.obs import USAGE, get_logger

load_dotenv()

log = get_logger(__name__)

PROMPT_PATH = PROMPTS_DIR / "classify_document.txt"

# Cap the OCR text we send so a 40-page deed doesn't blow the context window or
# the bill — the document *type* is almost always decided in the first page or
# two (title, parties, headers). Generous enough to capture the decisive header.
MAX_TEXT_CHARS = 12_000


class DocClassifyError(RuntimeError):
    """Raised when the classifier cannot be run (missing key / fatal API error)."""


def parse_llm_json(text: str | None) -> dict | None:
    """Lenient JSON extractor for LLM replies: strips ``` fences, then falls
    back to the outermost {...} slice. Returns None when no object parses.
    (Previously lived in pipeline/classify_notice.py.)"""
    if not text:
        return None
    text = text.strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = [l for l in text.split("\n") if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        try:
            s, e = text.find("{"), text.rfind("}") + 1
            if s >= 0 and e > s:
                obj = json.loads(text[s:e])
            else:
                return None
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


def _build_prompt(markdown: str, filename: str) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    body = template.replace("{taxonomy}", tax.render_taxonomy_for_prompt())
    text = (markdown or "").strip()[:MAX_TEXT_CHARS]
    header = f"Filename (a weak hint only): {filename}\n\n" if filename else ""
    return (
        body
        + "\n\n---\nDOCUMENT TEXT (OCR'd; may be noisy):\n\n"
        + header
        + text
    )


def _normalize(obj: dict | None) -> dict | None:
    """Validate the LLM verdict against the taxonomy.

    Returns ``{category, doc_type, confidence, reasoning}`` with ``doc_type``
    coerced to a known id or the UNKNOWN sentinel, and ``category`` derived from
    the doc type (we trust the taxonomy mapping over the model's category)."""
    if not isinstance(obj, dict):
        return None
    doc_type = tax.normalize_doc_type(obj.get("doc_type"))
    if doc_type and doc_type in tax.ALL_DOC_TYPE_IDS:
        category = tax.DOC_TYPE_TO_CATEGORY[doc_type]
    else:
        doc_type = tax.UNKNOWN_DOC_TYPE
        category = "unknown"
    conf = obj.get("confidence")
    if isinstance(conf, (int, float)):
        conf = max(0.0, min(1.0, float(conf)))
    else:
        conf = None
    reasoning = obj.get("reasoning")
    reasoning = reasoning[:500] if isinstance(reasoning, str) else ""
    return {
        "category": category,
        "doc_type": doc_type,
        "confidence": conf,
        "reasoning": reasoning,
    }


async def classify_document_text(markdown: str, filename: str = "") -> dict | None:
    """Classify one document's OCR text into the dossier taxonomy.

    Returns ``{category, doc_type, confidence, reasoning}`` (``doc_type`` is a
    taxonomy id or ``"unknown"``), or ``None`` if the model produced no usable
    verdict after retries. Raises :class:`DocClassifyError` on missing API key.
    """
    if not OPENROUTER_API_KEY:
        raise DocClassifyError("OPENROUTER_API_KEY missing")
    if not (markdown or "").strip():
        return {"category": "unknown", "doc_type": tax.UNKNOWN_DOC_TYPE,
                "confidence": 0.0, "reasoning": "empty document text"}

    payload = {
        "model": OPENROUTER_MODEL_DOC_CLASSIFY,
        "messages": [{"role": "user", "content": _build_prompt(markdown, filename)}],
        "max_tokens": 512,
        "temperature": 0.0,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        for attempt in range(MAX_RETRIES):
            try:
                async with session.post(
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status in (401, 403, 404):
                        body = await resp.text()
                        raise DocClassifyError(
                            f"OpenRouter {resp.status} for "
                            f"'{OPENROUTER_MODEL_DOC_CLASSIFY}': {body[:200]}"
                        )
                    if resp.status != 200:
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(2 ** attempt)
                        continue
                    data = await resp.json()
                    USAGE.record(data.get("usage"))
                    text = data["choices"][0]["message"]["content"]
                    return _normalize(parse_llm_json(text))
            except DocClassifyError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
        return None
