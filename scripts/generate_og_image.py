"""Generate the SITE-WIDE Open Graph / Twitter Card image for auctionscope.in.

Run from repo root:
    python scripts/generate_og_image.py

Produces web/og-image.png at 1200x630, the standard OG/Twitter size. This is
now only the FALLBACK card — the one a page gets when it has no card of its
own. Individual property pages get their own picture from
scripts/generate_property_og.py (see web/og-manifest.json).

STALE PALETTE — do not run without reading this first. The colours below
(cream #faf7f0 paper, yellow #ffd84d accent) predate the current brand, which
is cobalt #0052ff on near-white with Bricolage Grotesque / Inter / JetBrains
Mono (brand/, web/styles.css, marketing/templates/lib/tokens.css). Running
this as-is would overwrite the committed og-image.png with an off-brand card
that no longer matches the per-property ones. It also uses a second rendering
stack (PIL + Liberation fonts) rather than the Playwright + HTML/tokens
pipeline every other Auctionscope image goes through. Either port it onto the
tokens (preferred — one stack, one palette) or leave the committed PNG alone.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "og-image.png"

WIDTH, HEIGHT = 1200, 630

PAPER = (250, 247, 240)
INK = (26, 26, 26)
INK_SOFT = (58, 58, 58)
MUTED = (138, 138, 138)
ACCENT = (255, 216, 77)

SANS_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
SANS_REG = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
MONO = "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def main() -> None:
    img = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
    draw = ImageDraw.Draw(img)

    for x in range(0, WIDTH, 40):
        draw.line([(x, 0), (x, HEIGHT)], fill=(0, 0, 0, 8), width=1)
    for y in range(0, HEIGHT, 40):
        draw.line([(0, y), (WIDTH, y)], fill=(0, 0, 0, 8), width=1)

    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    for x in range(0, WIDTH, 40):
        overlay_draw.line([(x, 0), (x, HEIGHT)], fill=(0, 0, 0, 12), width=1)
    for y in range(0, HEIGHT, 40):
        overlay_draw.line([(0, y), (WIDTH, y)], fill=(0, 0, 0, 12), width=1)
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    label_font = _font(MONO, 22)
    draw.text((72, 64), "auctionscope.in", font=label_font, fill=MUTED)

    title_font = _font(SANS_BOLD, 128)
    title = "Auctionscope"
    accent_word = "scope"
    auction_part = title[: title.index(accent_word)]

    auction_w = draw.textlength(auction_part, font=title_font)
    accent_w = draw.textlength(accent_word, font=title_font)
    total_w = auction_w + accent_w

    title_x = (WIDTH - total_w) / 2
    title_y = 200

    bbox = title_font.getbbox(accent_word)
    accent_h = bbox[3] - bbox[1]
    pad_x, pad_y = 14, 10
    rect_x0 = title_x + auction_w - pad_x
    rect_y0 = title_y + bbox[1] - pad_y
    rect_x1 = title_x + total_w + pad_x
    rect_y1 = title_y + bbox[1] + accent_h + pad_y
    draw.rectangle([rect_x0, rect_y0, rect_x1, rect_y1], fill=ACCENT)

    draw.text((title_x, title_y), auction_part, font=title_font, fill=INK)
    draw.text((title_x + auction_w, title_y), accent_word, font=title_font, fill=INK)

    tag_font = _font(SANS_REG, 36)
    tagline = "AI-powered search for bank auctions"
    tag_w = draw.textlength(tagline, font=tag_font)
    draw.text(((WIDTH - tag_w) / 2, 400), tagline, font=tag_font, fill=INK_SOFT)

    foot_font = _font(MONO, 22)
    foot = "3,000+ Tamil Nadu auctions  ·  ask anything"
    foot_w = draw.textlength(foot, font=foot_font)
    draw.text(((WIDTH - foot_w) / 2, 480), foot, font=foot_font, fill=MUTED)

    draw.rectangle([0, HEIGHT - 8, WIDTH, HEIGHT], fill=ACCENT)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, "PNG", optimize=True)
    print(f"wrote {OUT}  ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
