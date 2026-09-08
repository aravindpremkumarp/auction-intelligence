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

2026-09-07: the catch-all "/(.*)" was replaced by the enumerated list of SPA
routes below. The destination stays "/" (the invariant this file was written
to protect); what changed is that a path which is NOT a client-side route no
longer resolves to the app. A catch-all sent every typo, dead link and probe
to the homepage with a 200, which made web/404.html unreachable and told
crawlers that infinitely many bogus URLs were real pages. The cost of
narrowing it is that a NEW client-side route must be added in two places —
web/app.js (pathForScreen/applyURLState) and vercel.json — or it 404s on
refresh, which is the exact failure mode of ISSUE-001. test_spa_routes_are_
rewritten below is the tripwire for that.
"""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every client-side route in web/app.js, in both slash forms. Vercel does not
# normalise a trailing slash for rewrite matching, so "/watchlist/" needs its
# own entry — without it a pasted URL with a trailing slash 404s.
SPA_ROUTES = [
    "/chat", "/chat/", "/chat/:id", "/chat/:id/",
    "/property/:id", "/property/:id/",
    "/watchlist", "/watchlist/",
    "/dossiers", "/dossiers/",
    "/lab", "/lab/",
]


def _load_config():
    return json.loads((REPO_ROOT / "vercel.json").read_text(encoding="utf-8"))


def test_spa_routes_are_rewritten():
    """Every client-side route must resolve to the app shell on a hard refresh.

    This replaces the old catch-all assertion. Deleting a line here is how
    ISSUE-001 comes back: the route keeps working on in-app navigation (the
    SPA never hits the network for it) and only breaks on refresh or a shared
    link, so it survives casual testing.
    """
    cfg = _load_config()
    sources = [r["source"] for r in cfg.get("rewrites", [])]
    missing = [route for route in SPA_ROUTES if route not in sources]
    assert not missing, (
        f"vercel.json is missing SPA rewrites for {missing}; those deep links "
        "404 on refresh/share. Add them (destination '/') alongside the route "
        "in web/app.js."
    )


def test_no_catch_all_rewrite():
    """A catch-all would resurrect the soft-404: unknown paths must 404.

    With no rewrite matching, Vercel serves web/404.html with a real 404
    status. A "/(.*)" rewrite instead serves the homepage with a 200, so bad
    links look like real pages to users and to crawlers.
    """
    cfg = _load_config()
    for rewrite in cfg.get("rewrites", []):
        assert rewrite["source"] not in {"/(.*)", "/:path*", "/(.*)/"}, (
            f"catch-all rewrite {rewrite['source']!r} makes every unknown URL "
            "a soft 404 and web/404.html unreachable. List the SPA routes "
            "explicitly instead (see SPA_ROUTES)."
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
