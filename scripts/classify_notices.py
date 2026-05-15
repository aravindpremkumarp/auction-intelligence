"""Legacy shim. The classifier now lives at pipeline/classify_notice.py.

The old cluster-count-only logic is the new module's Pass 1; the new
module adds a second LLM-on-markdown pass and writes additional fields
(classifier_pred, confidence, reasoning, model, classified_at).

Run the canonical entry point instead:
    python -m pipeline.classify_notice            # full run (pass 1 + 2)
    python -m pipeline.classify_notice --skip-llm # cluster-count only (legacy behavior)
    python -m pipeline.classify_notice --force    # re-score even when prior prediction exists
"""
from __future__ import annotations

import sys

from pipeline.classify_notice import run as canonical_run


def main() -> int:
    print("[classify_notices] forwarding to pipeline.classify_notice")
    # Keep legacy behavior (pass 1 only) unless explicitly requested otherwise.
    return canonical_run(skip_llm="--with-llm" not in sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
