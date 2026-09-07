"""Tripwires for two audit findings that regress silently (2026-09-07).

Both were found by measuring the built site, not by reading it, and both fail
in ways nobody notices while developing:

  * CLS 0.442 on every /property/<id> page. The prerendered #ssr-property
    block sits ahead of .app in the body and app.js removes it on boot; in
    normal flow that displaced .app and then snapped it back. The page looks
    perfect once loaded — the shift is only visible to a performance trace or
    to a user on a slow connection, and CLS is a Core Web Vitals ranking
    factor on the site's largest crawled surface.

  * White-on-accent at 3.19:1 in dark mode on the generated pages. Passes in
    light mode, which is what a developer usually has open.

These assert the shape of the fix, so a well-meaning edit (re-adding a layout
property inline, or a new button hardcoding #fff) fails here instead of in
production.
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
