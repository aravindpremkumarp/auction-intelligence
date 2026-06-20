"""Batch LangExtract runner with token-usage instrumentation + caching report.

Why this exists
---------------
LangExtract keeps only ``response.text`` and drops Gemini's ``usage_metadata``,
so neither cost nor cache hits are visible. This module patches the google-genai
client to accumulate usage across every call, then reports prompt / output /
**cached** tokens and an estimated cost.

Caching note
------------
Gemini 2.5 Flash does *implicit* prompt caching automatically: when consecutive
requests share a long identical prefix (here the ~5.5k-token canonical prompt +
few-shot examples, with only the trailing notice text varying) the prefix is
served from cache at a large discount. There is nothing to "turn on" — but it
only helps when calls are warm/sequential, which a batch run is. The
``cached_content_token_count`` printed below confirms it is actually firing.
(Explicit CachedContent does not fit LangExtract, which sends the whole prompt as
one ``contents`` string rather than a separable system prefix.)

Run:  python -m pipeline.langextract_run <file1.txt> [file2.txt ...]
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

from google.genai import models as _genai_models

from pipeline import langextract_examples as LX

# Gemini 2.5 Flash list price ($/1M tokens). Cached input bills ~25% of input.
PRICE_IN = 0.30
PRICE_OUT = 2.50
CACHED_INPUT_DISCOUNT = 0.25


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0          # billed (non-cached) input
    cached_tokens: int = 0          # input served from cache
    output_tokens: int = 0
    docs: int = 0
    _patched: bool = field(default=False, repr=False)

    def add(self, um) -> None:
        self.calls += 1
        total_in = getattr(um, "prompt_token_count", 0) or 0
        cached = getattr(um, "cached_content_token_count", 0) or 0
        self.cached_tokens += cached
        self.prompt_tokens += max(total_in - cached, 0)
        self.output_tokens += getattr(um, "candidates_token_count", 0) or 0

    @property
    def est_cost(self) -> float:
        return (
            self.prompt_tokens / 1e6 * PRICE_IN
            + self.cached_tokens / 1e6 * PRICE_IN * CACHED_INPUT_DISCOUNT
            + self.output_tokens / 1e6 * PRICE_OUT
        )

    def report(self) -> str:
        full_in = self.prompt_tokens + self.cached_tokens
        hit = (self.cached_tokens / full_in * 100) if full_in else 0.0
        no_cache = (full_in / 1e6 * PRICE_IN
                    + self.output_tokens / 1e6 * PRICE_OUT)
        per_doc = self.est_cost / self.docs if self.docs else 0.0
        return (
            f"docs={self.docs}  llm_calls={self.calls}\n"
            f"  input  : {self.prompt_tokens:,} billed + {self.cached_tokens:,} "
            f"cached  ({hit:.0f}% cache hit)\n"
            f"  output : {self.output_tokens:,}\n"
            f"  est cost: ${self.est_cost:.4f}  (${per_doc:.4f}/doc)  "
            f"vs ${no_cache:.4f} without cache\n"
            f"  → full corpus (496 docs): ~${per_doc * 496:.2f}"
        )


USAGE = Usage()


def install_usage_tracking() -> None:
    """Monkeypatch genai so every Gemini call records usage (idempotent)."""
    if USAGE._patched:
        return
    orig = _genai_models.Models.generate_content

    def patched(self, *a, **k):
        resp = orig(self, *a, **k)
        um = getattr(resp, "usage_metadata", None)
        if um is not None:
            USAGE.add(um)
        return resp

    _genai_models.Models.generate_content = patched
    USAGE._patched = True


def run(markdown: str):
    """Extract one notice (reuses the canonical prompt + examples)."""
    install_usage_tracking()
    USAGE.docs += 1
    return LX.extract(markdown)


def main(paths: list[str]) -> int:
    install_usage_tracking()
    for p in paths:
        md = open(p, encoding="utf-8").read()
        res = run(md)
        print(f"{p}: {len(res.extractions)} extractions")
    print("\n=== USAGE ===")
    print(USAGE.report())
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1:]))
