# AuctionScope — Logo & Brand Assets

A clean, fintech-style identity for **AuctionScope** (auctionscope.in): an auction
**gavel** mark on the brand-blue tile (`#0052ff`, the same Coinbase-blue accent used
across the product UI), paired with the **Inter** wordmark.

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
| Brand blue (accent) | `#0052ff` |
| Blue (hover/deep) | `#0046e6` |
| Ink (text) | `#0a0b0d` |
| Paper (canvas) | `#f6f7f9` |

Typeface: **Inter** (700 for the wordmark).

## Regenerating the PNGs

The PNGs are rasterized from the SVGs with headless Chromium. Re-run `render.py`
from the repo root (requires `playwright`) to regenerate every export.
