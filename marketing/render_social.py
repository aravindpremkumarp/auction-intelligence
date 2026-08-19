"""Render the social templates in marketing/templates/ to PNGs.

Static-image companion to the HyperFrames reels (which render with
``npx hyperframes render <template>``). Follows the same Playwright pattern
as brand/logo/render.py. Each template signals readiness by setting
``data-render-ready`` on <html> once fonts are loaded and entrance
animations have settled (see marketing/templates/lib/motion.js), so
screenshots never catch a mid-animation frame.

The scheduled poster flow injects fresh auction data by rewriting the
template's ``<script id="data" type="application/json">`` island — pass the
new island content via --data; nothing else in the template is touched.

Usage:
    python marketing/render_social.py                       # all static templates
    python marketing/render_social.py --template deal-of-the-day-1080
    python marketing/render_social.py --template city-carousel-1080x1350 \
        --data /tmp/chennai.json --out marketing/outputs/2026-07-12
"""

from __future__ import annotations

import argparse
import glob
import json
import pathlib
import re
import sys

# playwright is imported lazily inside render() so the module stays importable
# (for tests / --render-staged manifest logic) without the browser dep.

ROOT = pathlib.Path(__file__).resolve().parent
TEMPLATES = ROOT / "templates"

# 9:16 reels are HyperFrames compositions rendered by `npx hyperframes render`,
# not by this script.
STATIC_TEMPLATES = [
    "deal-of-the-day-1080",
    "price-drop-1080x1350",
    "city-carousel-1080x1350",
]

DATA_ISLAND = re.compile(
    r'(<script id="data" type="application/json">).*?(</script>)', re.S
)


def chromium_path() -> str | None:
    cands = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome") + glob.glob(
        "/opt/pw-browsers/chromium/chrome-linux/chrome"
    )
    return cands[0] if cands else None


def stage_name(template: str, out_name: str | None, index: int, multi: bool) -> str:
    """Filename for one .stage screenshot.

    Multi-stage templates (the carousel renders one .stage per slide) always get
    a numbered suffix — otherwise every slide overwrites the same PNG. When the
    caller supplied a name it is the stem, so a staged carousel lands as
    card-00-carousel_01.png … and never collides with another card.
    """
    if out_name and multi:
        return f"{pathlib.Path(out_name).stem}_{index:02d}.png"
    if out_name:
        return out_name
    return f"{template}_{index:02d}.png" if multi else f"{template}.png"


def stage_html(template: str, island: dict | None) -> tuple[str, pathlib.Path]:
    """Write <template>.html with `island` swapped into its #data script and
    return (file_uri, staged_path). Staged beside the real template so the
    relative lib/ asset paths (tokens.css, motion.css, motion.js) still
    resolve — otherwise the motion kit never loads and data-render-ready
    never fires."""
    src = TEMPLATES / f"{template}.html"
    if not src.exists():
        sys.exit(f"unknown template: {template} ({src} missing)")
    html = src.read_text(encoding="utf-8")
    if island is None:
        return src.as_uri(), src
    blob = json.dumps(island, ensure_ascii=False, indent=2)
    html, n = DATA_ISLAND.subn(rf"\g<1>\n{blob}\n\g<2>", html)
    if n != 1:
        sys.exit(f"{template}: expected exactly one #data island, found {n}")
    staged = TEMPLATES / f".{template}.staged.html"
    staged.write_text(html, encoding="utf-8")
    return staged.as_uri(), staged


# Webfont hosts the templates pull from. Cached per run rather than per render.
_FONT_HOSTS = ("https://fonts.googleapis.com/**", "https://fonts.gstatic.com/**")


def _install_font_cache(page) -> None:
    """Serve repeat webfont requests from memory for the life of this page.

    The browser re-requests the font CSS and files on every navigation, and each
    round trip costs far more than the screenshot. Caching the first response
    and replaying it keeps every card visually identical — same fonts, same
    bytes — while paying for them once. A fetch that fails is aborted rather
    than cached, so a transient error cannot poison later renders.
    """
    cache: dict[str, tuple[bytes, dict, int]] = {}

    def handler(route, request):
        hit = cache.get(request.url)
        if hit is not None:
            body, headers, status = hit
            route.fulfill(status=status, body=body, headers=headers)
            return
        try:
            response = route.fetch()
            body, headers = response.body(), response.headers
            cache[request.url] = (body, headers, response.status)
            route.fulfill(status=response.status, body=body, headers=headers)
        except Exception:  # noqa: BLE001 — offline/blocked: let the page fall back
            route.abort()

    for pattern in _FONT_HOSTS:
        page.route(pattern, handler)


def render_batch(template: str, items: list[tuple[dict, str]],
                 out_dir: pathlib.Path, viewport: tuple[int, int] = (1240, 900),
                 progress_every: int = 50) -> list[pathlib.Path]:
    """Render many islands through ONE browser.

    render() launches a browser per call, which is fine for the five cards a
    content batch stages and hopeless for the whole property inventory (664
    cold starts dominate the actual screenshots). This keeps one browser and
    one page alive and re-navigates per item.

    Two things keep the per-item cost near the screenshot itself rather than the
    network. The templates pull webfonts from Google Fonts, and re-fetching them
    per item dominated everything else — measured at ~26s per card against ~0.5s
    once fixed, which is the difference between four minutes and four hours over
    the property inventory:

      · Font responses are cached in-process and replayed to later navigations,
        so the fetch happens once per run instead of once per card.
      · Navigation goes to a per-item URL rather than goto()-then-reload(). The
        staged file has the same path every time, so a bare goto() to a URL the
        page already sits on may not re-navigate; the old fix was a reload(),
        which forced a second full network round. A unique query string
        guarantees a real navigation in one round instead of two.

    `items` is [(island_dict, out_name.png), …]. Single-stage templates only —
    the per-property OG card is one .stage by construction; a multi-stage
    template would need the numbering render() does and belongs there.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[pathlib.Path] = []
    if not items:
        print("render_batch: nothing to render")
        return written

    from playwright.sync_api import sync_playwright  # lazy: only needed to render

    staged: pathlib.Path | None = None
    with sync_playwright() as p:
        exe = chromium_path()
        browser = p.chromium.launch(**({"executable_path": exe} if exe else {}))
        page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
        _install_font_cache(page)
        try:
            for i, (island, out_name) in enumerate(items, start=1):
                url, staged = stage_html(template, island)
                # Unique URL per item: the staged file keeps one path, so a bare
                # goto() may not re-navigate. See the docstring — this replaces a
                # goto()+reload() pair and halves the navigations.
                page.goto(f"{url}?i={i}")
                page.wait_for_selector("html[data-render-ready]", timeout=30_000)
                stage = page.query_selector(".stage")
                if not stage:
                    sys.exit(f"{template}: no .stage element to screenshot")
                path = out_dir / out_name
                stage.screenshot(path=str(path))
                written.append(path)
                if progress_every and i % progress_every == 0:
                    print(f"  rendered {i}/{len(items)}")
        finally:
            browser.close()
            if staged is not None and staged.name.startswith("."):
                staged.unlink(missing_ok=True)
    print(f"rendered {len(written)} image(s) → {out_dir}")
    return written


def render(template: str, data_file: str | None, out_dir: pathlib.Path,
           out_name: str | None = None) -> list[pathlib.Path]:
    src = TEMPLATES / f"{template}.html"
    if not src.exists():
        sys.exit(f"unknown template: {template} ({src} missing)")

    html = src.read_text(encoding="utf-8")
    page_url = src.as_uri()
    out_dir.mkdir(parents=True, exist_ok=True)
    if data_file:
        island = json.dumps(json.loads(pathlib.Path(data_file).read_text(encoding="utf-8")),
                            ensure_ascii=False, indent=2)
        html, n = DATA_ISLAND.subn(rf"\g<1>\n{island}\n\g<2>", html)
        if n != 1:
            sys.exit(f"{template}: expected exactly one #data island, found {n}")
        # Stage beside the real template so its relative lib/ asset paths
        # (tokens.css, motion.css, motion.js) still resolve — otherwise the
        # motion kit never loads and data-render-ready never fires.
        staged = TEMPLATES / f".{template}.staged.html"
        staged.write_text(html, encoding="utf-8")
        page_url = staged.as_uri()

    written: list[pathlib.Path] = []

    from playwright.sync_api import sync_playwright  # lazy: only needed to render

    with sync_playwright() as p:
        exe = chromium_path()
        browser = p.chromium.launch(**({"executable_path": exe} if exe else {}))
        page = browser.new_page(viewport={"width": 1200, "height": 1500})
        page.goto(page_url)
        page.wait_for_selector("html[data-render-ready]", timeout=30_000)

        stages = page.query_selector_all(".stage")
        if not stages:
            sys.exit(f"{template}: no .stage elements to screenshot")
        multi = len(stages) > 1
        for i, stage in enumerate(stages, start=1):
            path = out_dir / stage_name(template, out_name, i, multi)
            stage.scroll_into_view_if_needed()
            stage.screenshot(path=str(path))
            written.append(path)
            print("rendered", path)
        browser.close()

    if data_file:
        pathlib.Path(page_url.removeprefix("file://")).unlink(missing_ok=True)
    return written


def render_staged(date_dir: pathlib.Path) -> list[pathlib.Path]:
    """Render every card the Poster staged for one run to a UNIQUE PNG.

    Reads the `cards` manifest from <date_dir>/drafts.json (each row carries
    its template + data-island path + auction_id) and renders each into
    <date_dir>/rendered/card-<NN>-<id>.png. This is what the content-poster
    workflow calls so finished card images ship with every run — no per-card
    command, no filename collisions (plain --template writes one file per
    template and would overwrite same-template cards)."""
    manifest = json.loads((date_dir / "drafts.json").read_text(encoding="utf-8"))
    cards = manifest.get("cards", [])
    out_dir = date_dir / "rendered"
    written: list[pathlib.Path] = []
    for c in cards:
        data = date_dir / c["data"]                              # cards/NN-id[-carousel].json
        # Name off the island's own stem, which the writers already keep unique
        # (01-D1 vs 01-D1-carousel vs 00-carousel). Keying on draft_index +
        # auction_id instead would give a draft's card and its property carousel
        # the same stem — card-01-D1.png beside card-01-D1_01.png, which is
        # unreadable and collides outright if the carousel is ever single-stage.
        name = f"card-{data.stem}.png"
        written += render(c["template"], str(data), out_dir, out_name=name)
    if not cards:
        print("no staged cards to render")
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--template", help="template name (default: all static templates)")
    ap.add_argument("--data", help="JSON file replacing the template's #data island")
    ap.add_argument("--out", default=str(ROOT / "outputs" / "social"),
                    help="output directory (default: marketing/outputs/social)")
    ap.add_argument("--render-staged", metavar="DATE_DIR",
                    help="render every card staged in DATE_DIR/drafts.json to DATE_DIR/rendered/")
    args = ap.parse_args()

    if args.render_staged:
        render_staged(pathlib.Path(args.render_staged))
        return

    names = [args.template] if args.template else STATIC_TEMPLATES
    if args.template and args.template not in STATIC_TEMPLATES:
        reels = sorted(p.stem for p in TEMPLATES.glob("*reel*.html"))
        if args.template in reels:
            sys.exit(
                f"{args.template} is a HyperFrames composition — render it with:\n"
                f"  python marketing/render_reel.py --template {args.template} --data <island.json>"
            )
    out_dir = pathlib.Path(args.out)
    for name in names:
        render(name, args.data, out_dir)


if __name__ == "__main__":
    main()
