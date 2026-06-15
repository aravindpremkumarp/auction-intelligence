"""
tests/e2e/conftest.py
---------------------
Gate for the live Razorpay test-mode E2E lane (design D5).

Unlike tests/api (which stubs Neo4j/Supabase/Razorpay), this lane talks to a
**real** Razorpay test-mode account and a **real** Neo4j, so it needs live
credentials. The gate is deliberately asymmetric:

- **In CI** (`CI` truthy): missing secrets **fail the run loudly** — a red build,
  never a silent skip. This is what keeps the payment path from rotting.
- **Locally** (no `CI`): missing secrets **skip with a clear reason**, so a
  contributor without keys can still run `pytest`.

This conftest imports only stdlib + pytest, so collection is safe even when the
api package's env is unset.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the repo root importable so `from api...` resolves (this lane has no
# stub conftest of its own, unlike tests/api).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

REQUIRED_SECRETS = [
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET",
    "NEO4J_URI",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
]


def _missing_secrets() -> list[str]:
    return [v for v in REQUIRED_SECRETS if not os.environ.get(v, "").strip()]


def _in_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}


def pytest_collection_modifyitems(config, items) -> None:  # noqa: ANN001
    missing = _missing_secrets()
    if not missing:
        return
    msg = "live Razorpay E2E requires secrets: " + ", ".join(missing)
    if _in_ci():
        # D5: do NOT skip in CI — a missing-secret build must go red.
        raise pytest.UsageError(msg + " — set them as CI secrets to run this lane.")
    skip = pytest.mark.skip(reason=msg + " (skipped locally)")
    for item in items:
        item.add_marker(skip)
