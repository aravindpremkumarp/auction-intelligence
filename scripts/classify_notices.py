"""Legacy shim. The classifier lives at pipeline/classify_notice.py.

Run the canonical entry point instead:
    python -m pipeline.classify_notice
"""
from __future__ import annotations

from pipeline.classify_notice import run as canonical_run


def main() -> int:
    print("[classify_notices] forwarding to pipeline.classify_notice")
    return canonical_run()


if __name__ == "__main__":
    raise SystemExit(main())
