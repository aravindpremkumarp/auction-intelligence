"""Structured-output client for the reader: one call, one Pydantic result.

What it pins down that the old path left to chance:

* ``temperature=0`` and a fixed ``seed``, so two reads of the same text are
  as alike as the provider allows;
* an explicit ``max_tokens`` and a ``finish_reason`` check — a response cut
  off mid-JSON raises :class:`Truncated` and the caller halves its chunk,
  instead of a parse of whatever prefix survived;
* three tiers of schema enforcement, because support differs by model and
  host on OpenRouter: ``json_schema`` (strict decoding) → ``json_object`` +
  Pydantic validation → plain text with fence stripping + Pydantic. The
  first tier the model accepts is used and reported in the result, and
  scripts/probe_structured_outputs.py records which tier each model reaches;
* one repair retry on a validation error, sending the error back;
* usage recorded into pipeline.langextract_run.USAGE so the eval's cost
  counter sees these calls too.

Reasoning is turned off for the models pipeline/extract_routing says so
(DeepSeek spends reasoning tokens from the answer's own budget and returns
nothing when it thinks too long), and a provider order can be pinned so
OpenRouter does not spread one batch over hosts with different quantisations.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from pipeline.reader.schema import strict_json_schema

TIERS = ("json_schema", "json_object", "text")
DEFAULT_MAX_TOKENS = 12000
DEFAULT_SEED = 7


class Truncated(RuntimeError):
    """The model hit max_tokens before finishing its JSON."""


class Unparseable(RuntimeError):
    """No tier produced JSON the schema accepts."""


@dataclass
class StructuredResult:
    parsed: BaseModel
    finish_reason: str | None
    usage: dict
    tier: str
    model: str
    raw: str = ""
    repaired: bool = False
    meta: dict = field(default_factory=dict)


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.S)


def strip_fences(text: str) -> str:
    """The JSON inside ``` fences, or the first {...} block, or the text."""
    t = _FENCE.sub("", text or "").strip()
    if t.startswith("{") or t.startswith("["):
        return t
    i, j = t.find("{"), t.rfind("}")
    return t[i:j + 1] if i != -1 and j > i else t


def _usage_dict(usage) -> dict:
    if usage is None:
        return {}
    details = getattr(usage, "prompt_tokens_details", None)
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "cached_tokens": (getattr(details, "cached_tokens", 0) or 0) if details else 0,
    }


def _record_usage(usage, model: str | None = None) -> None:
    """Into the shared counter — unless the OpenAI client is already patched
    by pipeline.langextract_run.install_usage_tracking, which records every
    call itself (counting here too would bill each call twice)."""
    try:
        from pipeline.langextract_run import USAGE
        if usage is not None and not USAGE._patched:
            USAGE.add_openai(usage, model)
    except Exception:  # pragma: no cover - accounting must never break a read
        pass


def call_cost(model: str, usage: dict) -> float:
    """$ for one call's usage dict, at the model's own price."""
    from pipeline.langextract_run import _prices
    pin, pout, pcache = _prices(model)
    cached = usage.get("cached_tokens", 0)
    billed = max(usage.get("prompt_tokens", 0) - cached, 0)
    return billed / 1e6 * pin + cached / 1e6 * pcache + usage.get("completion_tokens", 0) / 1e6 * pout


def default_client(timeout: float | None = None):
    """An OpenAI client pointed at OpenRouter, from the pipeline's env."""
    from openai import OpenAI
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    return OpenAI(
        api_key=key,
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        timeout=timeout if timeout is not None
        else float(os.environ.get("LANGEXTRACT_REQUEST_TIMEOUT_S", "300")),
        max_retries=1,
    )


def _response_format(tier: str, schema: type[BaseModel]) -> dict | None:
    if tier == "json_schema":
        return {"type": "json_schema",
                "json_schema": {"name": schema.__name__, "strict": True,
                                "schema": strict_json_schema(schema)}}
    if tier == "json_object":
        return {"type": "json_object"}
    return None


def _extra_body(reasoning_off: bool, provider_order: list[str] | None) -> dict:
    body: dict[str, Any] = {}
    if reasoning_off:
        body["reasoning"] = {"enabled": False}
    if provider_order:
        body["provider"] = {"order": list(provider_order), "allow_fallbacks": True}
    return body


def _tier_unsupported(err: Exception) -> bool:
    """Whether an API error means "this response_format is not supported",
    as opposed to a transient or auth failure."""
    msg = str(err).lower()
    return any(s in msg for s in ("response_format", "json_schema", "structured",
                                  "not supported", "unsupported", "invalid_request",
                                  "400"))


def call_structured(model_id: str, system: str, user: str,
                    schema: type[BaseModel], *, max_tokens: int = DEFAULT_MAX_TOKENS,
                    temperature: float = 0.0, seed: int | None = DEFAULT_SEED,
                    reasoning_off: bool | None = None,
                    provider_order: list[str] | None = None,
                    tier: str = "auto", client=None, timeout: float | None = None,
                    ) -> StructuredResult:
    """One structured call. ``tier`` is "auto" (try each in order) or one of
    TIERS. ``client`` is injectable for tests. Raises :class:`Truncated` when
    the answer hit max_tokens, :class:`Unparseable` when no tier yields JSON
    the schema accepts, and re-raises any other API error."""
    if reasoning_off is None:
        from pipeline.extract_routing import reasoning_off_for
        reasoning_off = reasoning_off_for(model_id)
    if provider_order is None:
        env = os.environ.get("OPENROUTER_EXTRACT_PROVIDER_ORDER", "")
        provider_order = [s.strip() for s in env.split(",") if s.strip()] or None
    client = client or default_client(timeout)
    tiers = list(TIERS) if tier == "auto" else [tier]
    last_err: Exception | None = None
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    for t in tiers:
        kwargs: dict[str, Any] = dict(model=model_id, messages=messages,
                                      max_tokens=max_tokens, temperature=temperature)
        if seed is not None:
            kwargs["seed"] = seed
        rf = _response_format(t, schema)
        if rf is not None:
            kwargs["response_format"] = rf
        extra = _extra_body(reasoning_off, provider_order)
        if extra:
            kwargs["extra_body"] = extra
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001 - classify below
            if t != tiers[-1] and _tier_unsupported(e):
                last_err = e
                continue
            raise
        _record_usage(getattr(resp, "usage", None), model_id)
        choice = resp.choices[0]
        finish = getattr(choice, "finish_reason", None)
        text = (choice.message.content or "") if choice.message else ""
        if finish == "length":
            raise Truncated(f"{model_id}: response hit max_tokens={max_tokens}")
        try:
            parsed = schema.model_validate_json(strip_fences(text))
            return StructuredResult(parsed, finish, _usage_dict(resp.usage), t,
                                    model_id, raw=text)
        except (ValidationError, ValueError) as e:
            # One repair round: the model sees its own error.
            repair = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": ("Your JSON did not match the schema: "
                                             f"{str(e)[:1500]}\nReturn the corrected "
                                             "JSON only.")}]
            try:
                resp2 = client.chat.completions.create(**{**kwargs, "messages": repair})
            except Exception as e2:  # noqa: BLE001
                last_err = e2
                continue
            _record_usage(getattr(resp2, "usage", None), model_id)
            c2 = resp2.choices[0]
            text2 = (c2.message.content or "") if c2.message else ""
            if getattr(c2, "finish_reason", None) == "length":
                raise Truncated(f"{model_id}: repair hit max_tokens={max_tokens}")
            try:
                parsed = schema.model_validate_json(strip_fences(text2))
                u = _usage_dict(resp.usage)
                for k, v in _usage_dict(resp2.usage).items():
                    u[k] = u.get(k, 0) + v
                return StructuredResult(parsed, getattr(c2, "finish_reason", None), u,
                                        t, model_id, raw=text2, repaired=True)
            except (ValidationError, ValueError) as e3:
                last_err = e3
                continue
    raise Unparseable(f"{model_id}: no tier produced valid {schema.__name__}: {last_err}")


def parse_json_text(text: str, schema: type[BaseModel]) -> BaseModel:
    """Validate raw model text against a schema (for cached transcripts)."""
    return schema.model_validate_json(strip_fences(text))


__all__ = ["call_structured", "StructuredResult", "Truncated", "Unparseable",
           "strip_fences", "parse_json_text", "default_client", "TIERS",
           "json"]
