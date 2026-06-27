# AuctionScope — Logo & Brand Assets

The identity for **Auctionscope** (auctionscope.in), matching the live web app: a
white **locate / scope reticle** (center dot, ring, four crosshair ticks) on the
cobalt-blue tile, paired with the **Inter** wordmark — "Auction" in dark slate and
"scope" in gray, set as one lowercase word.

## Files

| File | Use |
| --- | --- |
| `icon.svg` | Source vector for the app/avatar mark (square tile) |
| `wordmark.svg` | Source vector for the horizontal lockup (icon + "AuctionScope") |
| `icon-1024.png` | High-res square mark — favicons, app icons, general use |
| `linkedin-avatar-400.png` | **LinkedIn profile / company-page picture** (400×400) |
| `linkedin-banner-1584x396.png` | **LinkedIn personal background banner** (1584×396) |
| `wordmark-light.png` | Wordmark for light backgrounds |
| `wordmark-dark.png` | Wordmark for dark backgrounds |

## Uploading to LinkedIn

- **Profile / Company logo:** upload `linkedin-avatar-400.png`. LinkedIn crops avatars
  into a circle — the gavel is centered with padding so nothing important is clipped.
- **Background / cover banner (personal profile):** upload `linkedin-banner-1584x396.png`.
  Key text sits on the left so it stays clear of the profile photo overlap.
- For a **Company Page cover** (1128×191), crop the banner or ask and a dedicated size
  can be generated.

## Brand colors

| Token | Hex |
| --- | --- |
| Tile blue (light→deep) | `#3b74f2` → `#2a5fe6` |
| Wordmark "Auction" | `#1f2a37` |
| Wordmark "scope" | `#98a1ad` |
| Reticle | `#ffffff` |

Typeface: **Inter** (700 for the wordmark).

## Regenerating the PNGs

The PNGs are rasterized from the SVGs with headless Chromium. Re-run `render.py`
from the repo root (requires `playwright`) to regenerate every export.
