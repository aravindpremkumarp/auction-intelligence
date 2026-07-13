# Auctionscope social template pack

The production templates for plan §4 Move 7 (the HyperFrames-powered social
engine) — adapted from the motion-anything library (see `ATTRIBUTION.md`;
full library triage in `docs/marketing/motion-anything-catalog.md`).

Every template is a self-contained HTML doc on the Auctionscope tokens
(`lib/tokens.css`, synced from `web/styles.css`) with a
`<script id="data" type="application/json">` island holding sample data.
**The poster pipeline only ever rewrites the island** — layout, honesty
microcopy, and computed figures (like the price-drop %) live in the template.

### The `headline` field (the scroll-stopping hook)

The static cards and the carousel cover each read an optional **`headline`**
field — the compressed hook from the copy system (`copy-playbook.md` Part 1,
"hook surfaces"). When present it leads the card as the hero line and the
property title steps down to a supporting line; when absent the card falls
back to its title-led layout (so old data files render unchanged). The Poster
fills `headline` from each draft's `image_headline`
(`marketing_agents/poster.py` → `draft_to_island()`), so the hook that leads
the caption also leads the image. Grounding is preserved end to end: a field
present but empty/null (e.g. a missing EMD) **clears** its slot and the wrapper
hides — the card never shows a stale sample number (`lib/motion.js` binder).

## Static formats → PNG (`marketing/render_social.py`)

| Template | Size | Post type |
|---|---|---|
| `deal-of-the-day-1080.html` | 1080×1080 | Deal of the day card |
| `price-drop-1080x1350.html` | 1080×1350 | Re-auction price drop (drop % computed from the two notice prices) |
| `city-carousel-1080x1350.html` | 1080×1350 × N | "Cheapest X in [city] this week" — cover + slide per property + CTA |

```bash
python marketing/render_social.py                                  # all, sample data
python marketing/render_social.py --template deal-of-the-day-1080 \
    --data fresh.json --out marketing/outputs/2026-07-12           # pipeline use
```

The script waits for `data-render-ready` on `<html>` (set by `lib/motion.js`
after fonts + entrance animations settle, which also freezes looping effects)
so a screenshot never catches a mid-animation frame.

## Reel formats → MP4 (HyperFrames compositions)

| Template | Size | Content |
|---|---|---|
| `stats-reel-1080x1920.html` | 9:16, 12s | hook → live-inventory count-ups (`/stats`) → today's pick → logo outro |
| `evaluate-reel-1080x1920.html` | 9:16, 15s | hook → real buyer question → AI answer typed with citations → CTA outro |

```bash
npx hyperframes render marketing/templates/stats-reel-1080x1920.html
npx hyperframes render marketing/templates/evaluate-reel-1080x1920.html
```

These follow the HyperFrames composition contract (root `data-*`, clips,
one paused GSAP timeline registered at `window.__timelines.main`). All motion
is **seek-safe**: counters tween `textContent` with render-time modifiers and
the typewriter is staggered `gsap.set()`s — no `onUpdate`/rAF state, so every
frame renders identically no matter how the renderer seeks. GSAP is vendored
at `lib/gsap.min.js`.

Evaluate-reel content rule: the answer and citations must be **real product
output** (the honesty line in the template says so and stays).

## Conventions

- Numbers format Indian-style via `AS.formatINR` — `₹38.5 L`, `₹1.2 Cr`,
  en-IN grouping (`data-field-format="inr"`).
- One accent per format: deal = accent blue, price-drop = green, urgency = red.
- Fonts load non-blocking with system fallbacks, so an offline render degrades
  gracefully instead of hanging.
