"""LangExtract's own pre-flight alignment check over the few-shot examples.

tests/api/test_langextract_examples_grounded.py already asserts, statically, that
every example span is a verbatim substring of its own source text. This is the
complementary LIVE check: it runs the aligner LangExtract actually uses at
extraction time (``resolver.WordAligner`` over the tokenizer), which is what
assigns ``char_interval`` in production. A span can be an exact substring and
still fail to align — tokenization, not string search, decides.

Needs ``langextract`` installed (config/requirements.txt), so it skips where the
dep is absent, exactly like the rest of the pipeline suite.
"""
from __future__ import annotations

import pytest

lx_examples = pytest.importorskip(
    "pipeline.langextract_examples",
    reason="langextract is a pipeline-only dep (config/requirements.txt)")

# Non-exact (fuzzy / lesser) matches are expected and harmless: the example
# sources are MinerU markdown, so table pipes, unicode fractions and spacing
# make most spans align fuzzily rather than token-exactly. What must never
# happen is a FAILED span — one the aligner cannot place at all, which teaches
# the model a span that can never be grounded.
#
# The budget is a ratchet, not a target. It exists so a prompt edit that makes
# alignment materially worse shows up as a test failure instead of a quiet drop
# in char_interval quality across the corpus. Re-measure and lower it whenever
# an example is rewritten; raise it only with a reason.
MAX_NON_EXACT = 115


def test_no_example_span_fails_to_align():
    summary = lx_examples.validate_examples(level="warning")
    assert summary["failed"] == 0, (
        f"{summary['failed']} of {summary['total']} few-shot example spans "
        "cannot be aligned to their own source text — each one silently teaches "
        "the model an ungroundable span. Run with "
        "LANGEXTRACT_PROMPT_VALIDATION=error to see them.")


def test_non_exact_alignment_stays_within_budget():
    summary = lx_examples.validate_examples(level="warning")
    assert summary["non_exact"] <= MAX_NON_EXACT, (
        f"{summary['non_exact']} of {summary['total']} example spans align only "
        f"fuzzily (budget {MAX_NON_EXACT}). Fuzzy spans still ground, but a jump "
        "means an edit moved spans away from the source text — check the diff "
        "before raising the budget.")


def test_error_level_is_the_default():
    """Production must fail loudly on an unalignable example, not warn."""
    import inspect
    src = inspect.getsource(lx_examples.validate_examples)
    assert '"LANGEXTRACT_PROMPT_VALIDATION", "error"' in src, (
        "the default validation level must stay 'error' — at 'warning' a broken "
        "example degrades every extraction silently")
