# Campaign assets

Campaign-specific exports — social crops, ad creatives, one-off graphics, teardown
screenshots.

**Source brand assets live in [`../../brand/logo/`](../../brand/logo/)** (logo,
wordmark, LinkedIn avatar/banner, colors, and `render.py` to regenerate PNGs). Don't
duplicate those here — pull from there and export campaign variants into this folder.

## Brand quick reference

| Token | Hex |
| --- | --- |
| Tile blue (light→deep) | `#3b74f2` → `#2a5fe6` |
| Wordmark "Auction" | `#1f2a37` |
| Wordmark "scope" | `#98a1ad` |
| Accent (redesign) | `#2563eb` |

Typeface: **Inter**.

## Suggested structure (create as needed)

```
assets/
├── social/        square + landscape crops for LinkedIn / X / WhatsApp
├── teardowns/     screenshots of real product answers for market-teardown posts
└── ads/           creative variants if paid channels are tested
```
</content>
