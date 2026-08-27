# ChatGPT-style chat shell — mockup

A standalone mockup of the Auctionscope chat screen rebuilt on ChatGPT's layout.
Nothing is wired up: no API, no auth, no real data. It exists to be looked at and
argued with before any of it touches `web/`.

Open it:

```
open redesign/chatgpt-shell/index.html
```

One self-contained file. No build, no server.

## What it shows

Three states, matching the three reference screenshots:

| State | How to reach it |
| --- | --- |
| Empty, sidebar expanded | default on load |
| Empty, collapsed icon rail | the sidebar toggle, or **Sidebar** in the demo bar |
| Conversation | type anything, click a suggestion, or **Conversation** in the demo bar |

The floating pill at the top is mockup-only scaffolding (**Empty / Conversation /
Sidebar / Theme**). It goes away if this lands in the app.

## What was copied from ChatGPT

- **Collapsing sidebar.** One grid whose first column animates between a 52px icon
  rail and a 260px sidebar. Collapsed, the expand control moves to the top bar.
- **Centred empty state.** Greeting, then the input pill, then the suggestion rows,
  all stacked in the middle of the screen. The composer physically moves to the
  bottom dock once a conversation starts.
- **Pill composer.** Fully rounded, 52px tall: `+`, the text field, Think, dictate,
  and a blue circular send button.
- **Message treatment.** User turns are blue rounded bubbles on the right;
  answers are plain text on the left with copy / share / regenerate / more
  underneath. No avatars, no cards around the answer.
- **Sidebar structure.** Wordmark, primary nav, a section of saved things, a
  section of chats, account row pinned at the bottom.

## What deliberately differs

- **Property cards render inline in the answer.** Matches are the product, so they
  can't live in a separate pane the way the current app does it, and ChatGPT has no
  equivalent. They're bordered rows inside the assistant turn.
- **Nav items are ours** — Watchlist, Dossiers, Alerts — not Library / Scheduled /
  Plugins.
- **Light theme included.** ChatGPT's dark is the reference, but the app supports
  both, so every colour is a token with a light counterpart.

## Open questions before this becomes real

1. **The results pane.** Today the chat screen has a third column listing matches.
   This mockup drops it in favour of inline cards. That is the biggest behavioural
   change here and worth deciding on its own.
2. **Mobile.** The sidebar becomes an overlay drawer, and the composer drops the
   dictate button and the "Think" label to keep the text field usable. The app's
   current bottom tab bar has no place in this layout yet.
3. **Tokens.** Colours here are ChatGPT's neutrals, not `web/styles.css`'s Coinbase
   palette. Adopting this means picking which one wins.

## Verified

Rendered at 1440x900 and 390x844, light and dark, in all three states. No console
errors. (Google Fonts is blocked in the sandbox, so screenshots fall back to the
system sans; the font stack is correct.)
