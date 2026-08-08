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


def test_csp_allows_blob_media():
    """The /social review page plays staged reels as blob: URLs.

    The MP4s are admin-gated, so a <video src> can't carry the bearer token —
    the page fetches with auth and plays the result locally. With only
    `default-src 'self'` to fall back on, the browser blocks blob: media and
    every reel silently fails to play. Every CSP header block needs media-src.
    """
    cfg = _load_config()
    for block in cfg.get("headers", []):
        for header in block.get("headers", []):
            if header.get("key") != "Content-Security-Policy":
                continue
            directives = dict(
                (d.strip().split(" ", 1) + [""])[:2]
                for d in header["value"].split(";") if d.strip()
            )
            assert "media-src" in directives, (
                f"CSP for {block['source']!r} has no media-src, so blob: video "
                "is blocked by the default-src fallback"
            )
            assert "blob:" in directives["media-src"], (
                f"CSP media-src for {block['source']!r} must allow blob:"
            )


def test_frame_src_allows_r2_for_pdf_notices():
    """PDF sale notices render in an <iframe src="<R2 url>">.

    web/review.html renders images with <img> but PDFs with an iframe
    (renderGalleryRight / the markdown source card). R2 was listed in img-src
    only, so images loaded and every PDF notice showed a broken frame — the
    file itself was fine (200, application/pdf). object-src is 'none', so an
    <object>/<embed> fallback is not an option: frame-src is the only route.

    Every CSP block needs it, not just the site-wide one — /review_extraction
    ships its own complete CSP that overrides the general rule.
    """
    cfg = _load_config()
    r2 = "https://pub-69a65ab57d8845f09fe6384b980fbe0b.r2.dev"
    seen = 0
    for block in cfg.get("headers", []):
        for header in block.get("headers", []):
            if header.get("key") != "Content-Security-Policy":
                continue
            directives = dict(
                (d.strip().split(" ", 1) + [""])[:2]
                for d in header["value"].split(";") if d.strip()
            )
            if r2 not in directives.get("img-src", ""):
                continue  # this block doesn't serve notice media at all
            seen += 1
            assert r2 in directives.get("frame-src", ""), (
                f"CSP for {block['source']!r} allows R2 in img-src but not "
                "frame-src, so PDF notices are blocked in the review viewer"
            )
    assert seen, "no CSP block references the R2 bucket — did the origin change?"
