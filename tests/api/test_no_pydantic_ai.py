"""
tests/api/test_no_pydantic_ai.py
--------------------------------
The app no longer depends on pydantic-ai, and this keeps it that way.

Why a static scan rather than "the suite passes": CI installs `pydantic-evals`
for the eval-tooling tests, and pydantic-evals brings `pydantic-ai-slim` with
it. So in CI `import pydantic_ai` succeeds, and a stray import in app code
would pass every test and then crash the production deploy, whose lock file
does not carry the package. Reading the source is the only check that sees
the difference.

`evals/` is exempt: it is dev tooling that runs where pydantic-evals is
installed (its LLM judge builds a pydantic-ai model).
"""
from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

#: Everything that ships to, or runs against, production.
_APP_DIRS = ("api", "pipeline", "scripts", "sources", "scoring", "tracking",
             "marketing_agents", "scrapers")


def _imports_pydantic_ai(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        else:
            continue
        if any(n == "pydantic_ai" or n.startswith("pydantic_ai.") for n in names):
            return True
    return False


def test_app_code_never_imports_pydantic_ai() -> None:
    offenders = [
        str(path.relative_to(_ROOT))
        for d in _APP_DIRS
        for path in sorted((_ROOT / d).rglob("*.py"))
        if _imports_pydantic_ai(path)
    ]
    assert offenders == [], f"pydantic-ai is not a production dependency: {offenders}"


def test_production_requirements_do_not_list_it() -> None:
    for name in ("requirements.txt", "requirements.lock"):
        lines = (_ROOT / name).read_text(encoding="utf-8").splitlines()
        pins = [ln.split("#", 1)[0].strip().lower() for ln in lines]
        assert not any(p.startswith(("pydantic-ai", "pydantic_ai")) for p in pins), name


def test_the_scan_would_catch_one() -> None:
    """The scan must actually see both import shapes, or it passes vacuously."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        a = Path(tmp) / "a.py"
        a.write_text("import pydantic_ai\n")
        b = Path(tmp) / "b.py"
        b.write_text("from pydantic_ai.messages import TextPart\n")
        c = Path(tmp) / "c.py"
        c.write_text("from pydantic import BaseModel\n")
        assert _imports_pydantic_ai(a) and _imports_pydantic_ai(b)
        assert not _imports_pydantic_ai(c)
