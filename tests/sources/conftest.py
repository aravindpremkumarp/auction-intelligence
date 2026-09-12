"""Make the repo root importable so ``import sources`` resolves when this
directory is run on its own — the same trick ``tests/api/conftest.py`` uses."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
