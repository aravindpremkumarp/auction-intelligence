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

from playwright.sync_api import sync_playwright

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


def render(template: str, data_file: str | None, out_dir: pathlib.Path) -> list[pathlib.Path]:
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
            name = f"{template}_{i:02d}.png" if multi else f"{template}.png"
            path = out_dir / name
            stage.scroll_into_view_if_needed()
            stage.screenshot(path=str(path))
            written.append(path)
            print("rendered", path)
        browser.close()

    if data_file:
        pathlib.Path(page_url.removeprefix("file://")).unlink(missing_ok=True)
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--template", help="template name (default: all static templates)")
    ap.add_argument("--data", help="JSON file replacing the template's #data island")
    ap.add_argument("--out", default=str(ROOT / "outputs" / "social"),
                    help="output directory (default: marketing/outputs/social)")
    args = ap.parse_args()

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
