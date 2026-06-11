"""Tripwire for the SPA deep-link 404 (ISSUE-001, six prior fix attempts).

History (see git log -- vercel.json): with "cleanUrls": true, a rewrite
destination of "/index.html" returns NOT_FOUND at the Vercel edge because
cleanUrls hides the .html path (PR #50 curl evidence, re-confirmed live on
2026-06-11 when every /property/:id deep link served 404.html). The working
combination is a catch-all rewrite whose destination is "/" — the clean URL
that index.html actually resolves to.

The breakage was masked for a month because web/404.html was a full copy of
the app; commit 08285413 replaced it with a small dead-end page and every
shared property link went user-visibly dead. If you change vercel.json
routing, verify deep links by curl against a real Vercel preview deployment —
local servers do not honor vercel.json.
"""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_config():
    return json.loads((REPO_ROOT / "vercel.json").read_text(encoding="utf-8"))


def test_catch_all_rewrite_present():
    cfg = _load_config()
    sources = [r["source"] for r in cfg.get("rewrites", [])]
    assert "/(.*)" in sources, (
        "vercel.json must keep the catch-all SPA rewrite; without it every "
        "deep link (/property/:id, /chat, /watchlist) 404s on refresh/share."
    )


def test_rewrite_destination_is_not_index_html():
    cfg = _load_config()
    clean_urls = cfg.get("cleanUrls", False)
    for rewrite in cfg.get("rewrites", []):
        if clean_urls:
            assert rewrite["destination"] != "/index.html", (
                f"rewrite {rewrite['source']!r} -> /index.html is NOT_FOUND at "
                "the Vercel edge when cleanUrls is true (cleanUrls hides the "
                ".html path). Use '/' as the destination."
            )


def test_spa_fallback_404_exists():
    # Defense-in-depth: if rewrites ever silently stop applying again (it has
    # happened twice), Vercel serves web/404.html for unknown paths. It must
    # at least exist and link users back into the app.
    page = (REPO_ROOT / "web" / "404.html").read_text(encoding="utf-8")
    assert 'href="/"' in page, "404.html must link back to the app root"
