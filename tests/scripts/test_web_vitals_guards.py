"""Tripwires for audit findings that regress silently (2026-09-07).

All were found by measuring the built site, not by reading it, and all fail in
ways nobody notices while developing:

  * CLS 0.442 on every /property/<id> page. The prerendered #ssr-property
    block sits ahead of .app in the body and app.js removes it on boot; in
    normal flow that displaced .app and then snapped it back. The page looks
    perfect once loaded — the shift is only visible to a performance trace or
    to a user on a slow connection, and CLS is a Core Web Vitals ranking
    factor on the site's largest crawled surface.

  * White-on-accent at 3.19:1 in dark mode on the generated pages. Passes in
    light mode, which is what a developer usually has open.

  * Two render-blocking third parties in <head>. On a fast connection they
    cost a few hundred ms and look fine; when the CDN is slow or blocked the
    page stays BLANK until it times out (measured: 12.5s, nothing painted).
    Developers rarely see the bad case; users on a poor connection only see
    the bad case.

These assert the shape of the fix, so a well-meaning edit (re-adding a layout
property inline, a new button hardcoding #fff, or moving a script back into
the head) fails here instead of in production.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STYLES = REPO_ROOT / "web" / "styles.css"
GENERATED_ROOTS = ("web/guides", "web/compare", "web/bank-auctions")


def _ssr_rule() -> str:
    css = STYLES.read_text(encoding="utf-8")
    m = re.search(r"#ssr-property\s*\{(.*?)\}", css, re.S)
    assert m, "web/styles.css has no #ssr-property rule — the CLS fix is gone"
    return m.group(1)


def test_ssr_block_is_out_of_flow() -> None:
    """The whole fix is that the block never occupies layout space."""
    rule = _ssr_rule()
    assert re.search(r"position\s*:\s*fixed", rule), (
        "#ssr-property must stay position:fixed. In normal flow it sits ahead "
        "of .app, so app.js removing it shifts the page (measured CLS 0.442)."
    )


def test_ssr_block_overrides_inline_layout() -> None:
    """Deployed pages carry the old inline max-width/margin and cannot be
    regenerated without live inventory, so the stylesheet has to outrank them."""
    rule = _ssr_rule()
    for prop in ("max-width", "margin", "padding"):
        m = re.search(rf"{prop}\s*:[^;]*;", rule)
        assert m and "!important" in m.group(0), (
            f"#ssr-property's {prop} needs !important: pages already deployed "
            f"set it inline, and without it they keep the 0.442 shift."
        )


def test_prerender_does_not_reintroduce_inline_layout() -> None:
    """The generator must emit paint-level styles only."""
    src = (REPO_ROOT / "scripts" / "prerender_properties.py").read_text(encoding="utf-8")
    m = re.search(r"'<div id=\"ssr-property\" style=\"([^\"]*)", src)
    assert m, "the ssr-property div in prerender_properties.py changed shape"
    inline = m.group(1)
    for prop in ("max-width", "padding", "margin", "position"):
        assert prop not in inline, (
            f"{prop!r} is back in the inline style of #ssr-property. Layout "
            f"belongs in web/styles.css — inline layout here is what caused "
            f"the CLS regression."
        )


def _page_css() -> str:
    import scripts.build_landing_pages as blp
    return blp.PAGE_CSS


def test_dark_mode_defines_on_accent() -> None:
    """Both dark blocks must flip --on-accent; white on #5b8bff is 3.19:1."""
    css = _page_css()
    dark_blocks = re.findall(r"--accent:#5b8bff;(.*?)\}", css, re.S)
    assert dark_blocks, "the dark accent moved — re-check the contrast fix"
    for block in dark_blocks:
        assert "--on-accent:#0a0b0d" in block, (
            "a dark block sets --accent:#5b8bff without --on-accent:#0a0b0d, "
            "so white text lands on it at 3.19:1 (WCAG AA needs 4.5:1)."
        )


def test_accent_buttons_use_the_variable() -> None:
    """Hardcoding #fff on the accent is exactly how this broke the first time."""
    css = _page_css()
    bad = re.findall(r"background:var\(--accent\);color:#fff", css)
    assert not bad, (
        "a rule paints white directly on var(--accent). Use var(--on-accent) "
        "so the dark theme can flip it to near-black."
    )


def test_generated_pages_define_every_on_accent_they_use() -> None:
    """An undefined custom property makes the label unreadable rather than
    just low-contrast, so the built pages are checked too — they are patched
    in place (no live inventory here) and can drift from the generator."""
    offenders = []
    for root in GENERATED_ROOTS:
        for page in (REPO_ROOT / root).rglob("index.html"):
            text = page.read_text(encoding="utf-8")
            if "var(--on-accent)" in text and not re.search(r"--on-accent\s*:", text):
                offenders.append(str(page.relative_to(REPO_ROOT)))
    assert not offenders, f"pages use --on-accent without defining it: {offenders[:5]}"


# ── render-blocking third parties ───────────────────────────────────────────

FONTS_HOST = "fonts.googleapis.com/css2"
SUPABASE_CDN = "cdn.jsdelivr.net/npm/@supabase"


NOSCRIPT = re.compile(r"<noscript>.*?</noscript>", re.S | re.I)

# Staff tools, all Disallow-ed in robots.txt. They load auth.js in <head> on
# purpose — they gate on a session before rendering anything — so the supabase
# script has to stay in the head above it. First paint does not matter on a
# page whose whole job is to refuse anonymous visitors, and reordering their
# auth would be a behaviour change for no user-facing gain.
INTERNAL_TOOLS = {"web/admin.html", "web/review.html", "web/social.html",
                  "web/review_extraction.html"}


def _all_html() -> list[Path]:
    return sorted((REPO_ROOT / "web").rglob("*.html"))


def _public_html() -> list[Path]:
    return [p for p in _all_html()
            if str(p.relative_to(REPO_ROOT)) not in INTERNAL_TOOLS]


def test_no_render_blocking_font_stylesheet() -> None:
    """Every fonts <link> must use the non-blocking media=print pattern.

    A plain <link rel=stylesheet> to fonts.googleapis.com puts a third party on
    the critical path: first paint waits for it, so a slow or blocked CDN shows
    a blank page rather than fallback text.

    The <noscript> copy is deliberately a plain stylesheet — it only applies
    when scripts are off, where the onload swap could never fire — so those
    blocks are stripped before scanning rather than filtered afterwards.
    """
    blocking = []
    for page in _all_html():
        body = NOSCRIPT.sub("", page.read_text(encoding="utf-8"))
        for link in re.findall(r"<link\b[^>]*>", body):
            if FONTS_HOST not in link or 'rel="stylesheet"' not in link:
                continue
            if 'media="print"' not in link:
                blocking.append(f"{page.relative_to(REPO_ROOT)}: {link[:80]}")
    assert not blocking, (
        "render-blocking font stylesheets found:\n  " + "\n  ".join(blocking[:5])
        + "\nUse: preload + media=\"print\" onload=\"this.media='all'\" + <noscript>."
    )


def test_font_links_keep_display_swap() -> None:
    """Without display=swap the async pattern would hide text until fonts load."""
    missing = [
        str(page.relative_to(REPO_ROOT))
        for page in _all_html()
        for url in re.findall(rf"https://{re.escape(FONTS_HOST)}[^\"')]*",
                              page.read_text(encoding="utf-8"))
        if "display=swap" not in url
    ]
    assert not missing, f"font URLs without display=swap: {sorted(set(missing))[:5]}"


def test_supabase_loads_after_head_and_before_auth() -> None:
    """auth.js reads window.supabase at top level, so order is load-bearing.

    The script was moved out of <head> (where it blocked first paint) to just
    above auth.js. Moving it back into the head reintroduces the block; moving
    it below auth.js — or adding defer while auth.js stays sync — breaks login
    silently, because auth.js guards on window.supabase and just gives up.

    Scoped to public pages: the staff tools in INTERNAL_TOOLS keep both scripts
    in the head by design (see that constant).
    """
    offenders = []
    for page in _public_html():
        text = page.read_text(encoding="utf-8")
        if SUPABASE_CDN not in text:
            continue
        head_end = text.find("</head>")
        sb, auth = text.find(SUPABASE_CDN), text.find('src="/auth.js"')
        if auth < 0:
            continue  # a page that loads supabase without auth.js is not our case
        if not (head_end < sb < auth):
            offenders.append(str(page.relative_to(REPO_ROOT)))
    assert not offenders, (
        "supabase must load after </head> and before auth.js on: "
        f"{offenders[:5]} ({len(offenders)} pages)"
    )
