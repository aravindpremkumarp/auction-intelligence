# Auctionscope — UI redesign

A clean, minimal, professional reskin of the Auctionscope web app, moving away
from the original hand-drawn "sketchbook" style (cream paper, grid texture,
script fonts, yellow highlighter accents, doodle thumbnails) toward a modern
SaaS / fintech look (think Linear / Stripe / Vercel dashboards).

This is a **self-contained, vanilla HTML/CSS/JS prototype** of all four screens
on the new system. It is intended to be reviewed, then wired onto the real
`web/index.html`.

## Files

| File              | Purpose                                                             |
| ----------------- | ------------------------------------------------------------------- |
| `auctionscope.html` | The connected, responsive prototype — all four screens.           |
| `auctionscope.css`  | Design tokens + every component (themeable, light/dark).          |
| `screens.css`       | Page layouts + responsive rules (sits on top of the components).  |
| `prototype.js`      | Mock TN auction data + all interactions (navigation, filtering, chat, save). |

Open `auctionscope.html` directly in a browser — no build step.

## Design system

- **Accent** — calm blue `#2563eb` (hover `#1d4ed8`); single accent, no yellow.
- **Neutrals** — white surfaces on a slate-tinted canvas (`#ffffff` / `#f8fafc`),
  hairline borders `#e2e8f0`, text `#0f172a` → `#475569` → `#94a3b8`.
  Dark theme on `#0b1120` / `#111827`.
- **Type** — Inter, one family, with a clear scale; no script or mono labels.
- **Personality** — soft & friendly: 12–16px radii, pill chips, soft layered
  shadows instead of hard outlines, an 8px-based spacing system.
- **Light + dark** via `data-theme` on `<html>` (toggle top-right).

## Screens

- **Search / Home** — AI search hero, suggestion chips, a collapsible filter bar
  (search + a Filters toggle with an active-count badge + Clear all; Sort lives
  in the results meta row), and a grid of property cards (Variant A — list row).
- **Results / Chat** — chat-history sidebar grouped by date, the conversation
  with AI replies + sources, and a live matches panel. On tablet/mobile these
  collapse into a `History · Conversation · Matches` segmented control.
- **Property detail** — fact grid, price history, documents, Save to watchlist,
  Copy link, and an "Ask about this property" chat box.
- **Watchlist** — saved properties grouped by urgency, with an empty state.

## Responsive

Verified down to a 390px viewport: the desktop top-nav swaps to a fixed bottom
tab bar, grids collapse to a single column, touch targets hit 44px, the filter
panel stacks full-width, and there is no horizontal overflow.

## Adopting it on the real app

The CSS is built on the existing class names so it can drop onto the current
markup. To wire it onto `web/index.html`: swap `styles.css` for
`auctionscope.css` + `screens.css`, replace the doodle thumbnails with the
`#i-*` icon symbols defined at the top of `auctionscope.html`, and update markup
class hooks where they differ. The existing `app.js` / `auth.js` logic stays —
this is a skin.
