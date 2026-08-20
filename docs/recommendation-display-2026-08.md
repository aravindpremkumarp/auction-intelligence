# Recommendation display — design decisions (Aug 2026)

How property recommendations reach the buyer: the card, and the panel they
live in. **Nothing here is built yet.** Companion to
`docs/chat-agent-middleware-2026-08.md`; both land when the chat agent moves
onto the Deep Agents harness.

**Mockups** (live rows from the graph, queried 20 Aug 2026):

- Cards — <https://claude.ai/code/artifact/b1103198-b076-4c3c-a77e-8a4027da5a59>
- Panel — <https://claude.ai/code/artifact/c2cad57d-f360-49cf-bfd0-adb50af0dc86>

---

## Where we are today

Two channels that never speak:

1. **Prose** — the model writes an answer citing `auction_id`s.
2. **Cards** — `api/tool_returns.py::split_ui_overflow` pulls `_ui_results`
   *out* of the model-visible tool return and hands full rows straight to the
   UI. The panel renders rows the model never sees. (Correct call — it keeps
   heavy payloads out of context.)

The only bridge is citation order: the panel follows whichever ids the model
cites, best-first. And because the panel is browser state the agent cannot
see, `inject_panel_selection` in `api/agent.py` whispers the visible
`panel_auction_ids` *back* into the prompt so "compare these" resolves.

Card fields today (`web/app.js::propCardHtml`): type thumbnail, title,
location, bank, date, price, price-drop badge when re-auction, urgency
countdown, save button.

**Three gaps**

- Cards show facts, not reasons. Nothing says why this property is listed, or
  why it ranks first. The model knows; the card can't hear it.
- Every card weighs the same. A property that dropped 30% across two failed
  auctions looks identical to a fresh full-price listing.
- `scoring/auction_scorer.py` (ten dimensions) is offline-only. The richest
  signal in the repo never reaches a buyer.

---

## Part 1 — Reason-first cards

**The core move:** stop asking the synthesis call for prose about properties;
have it return a **typed recommendation object**. The call already runs, so
this is a response-format change, not a new model call.

### The object

```json
{
  "summary": "3 of 20 worth a look. Only 3 list a size.",
  "scope": {"city": "Chennai", "max_price": 4000000},
  "ranked_by": "budget fit, then what the data can support",
  "picks": [{
    "auction_id": "837057",
    "rank": 1,
    "reason": "Cheapest of all 20, and 20 days before bids close.",
    "badges": [{"text": "lowest reserve", "tone": "good"},
               {"text": "size not recorded", "tone": "neutral"}],
    "derived": {"price_per_sqft": null, "days_to_deadline": 20},
    "unknowns": ["extent", "encumbrance", "owner_match"]
  }],
  "why_not": [{"auction_id": "837061", "cut": "same bank and date as #1"}],
  "next_filter": {"label": "closing within 10 days", "count": 6}
}
```

**Why typed, not markdown:** prose must be parsed to render and cannot be
checked. A typed object can — the `AnswerGate` middleware verifies every
number in `derived` against the tool result before a card paints, and
`unknowns` makes gaps a *field* rather than something the model might forget
to mention.

### What it buys

| | |
|---|---|
| **A reason per card** | One line rendered on the card. Turns a list into a recommendation. ~25 output tokens per property. |
| **Adaptive shape** | Agent picks from the result set: 1 → rich card, 3–5 → comparison, 20+ → grouped summary with facets. Today all three render identically. |
| **Deal-shape badges** | `price dropped twice`, `deadline in 3 days`, `only one under ₹30L here`, `same borrower as 2 others`. |
| **Ranking transparency** | `ranked_by` names the axis. Silent ranking is the fastest way to lose trust in a recommender. |
| **Why-not list** | Near-misses plus the one filter that cut each. Reads as honesty; doubles as a narrowing hint. |
| **Uncertainty column** | The pre-bid check's verified / unknown / concerning states, brought forward into browsing — the natural on-ramp from free chat to the paid check. |

### Data finding that drove this

Of 20 live Chennai auctions under ₹40L, **only 3 record a size** and
`property_type` is null on several. Entity coverage is genuinely uneven —
partly because some sale notices carry limited data at source, not only
because extraction missed it.

Cards look uniform today partly because they hide that unevenness. **Surfacing
it beats papering over it**: a buyer told "only 3 of the 20 are comparable"
decides better than one shown 20 identical-looking cards. That single sentence
is worth more than twelve more cards.

---

## Part 2 — The one-room panel

**The inversion:** the panel stops mirroring the *chat* and starts mirroring
the *agent's state*. The chat becomes commentary on it.

### Two zones

- **Shelf** — what the buyer kept. Durable; survives topic switches and
  sessions. This is where background work happens.
- **Matches** — the current result set. Changes every turn.

A buyer's real journey is "search many times, keep three." Today the keeping
happens in their head or the watchlist; the panel should hold it, visibly,
beside the volatile results.

### Every click is a typed message

| Buyer does | Agent receives | Panel shows |
|---|---|---|
| Taps 📌 | `pin(837057)` | Card moves to the shelf, stays through the next search |
| Taps ✕ | `dismiss(831476, reason?)` | Greys out with the reason; ↺ restores |
| Taps × on a chip | `scope.drop(max_price)` | Chip goes, matches re-run, count updates |
| Taps card body | `focus(823287)` | Expands **in place** — no page change |
| Taps "Check this property" | `start_check(823287)` | Todo list appears on the card and ticks through |
| Types the one-time code | `resume(otp)` | Paused step turns green; the rest continues alone |

**This kills the `inject_panel_selection` workaround.** One source of truth,
read by both the screen and the agent — nothing needs whispering back.

Dismissals are signal, not just UI state: two ground-floor flats dismissed in
a row, and the agent can ask whether to skip ground floors entirely.

### Cards that fill themselves in

Pin a property → a background sub-agent enriches it (locality context,
guideline value) and the card gains badges **asynchronously** while the
conversation continues. The panel becomes a place where work completes over
minutes rather than a snapshot of one turn. No ReAct loop can do this;
background sub-agents can.

### Where the two products meet

A pinned card grows a "Check this property" button → starts a check-agent run
→ the card shows todo progress, pauses visibly for the OTP, and the
three-state checklist lands on the card. Browsing flows into diligence without
leaving the conversation.

### During a turn, the panel narrates

`searched · 20 match | compared · 3 | checking Ambattur…` — the typed plan
from the tiered loop, rendered as progress instead of silence. Free; the
object already exists.

---

## One anti-idea, recorded deliberately

**Do not let the agent generate the UI itself** (per-turn generative HTML).
The agent decides *what* to show; fixed components decide *how* it is drawn.
That line is what keeps the screen testable, consistent, and impossible to
fabricate.

---

## Build order

| Step | Needs | Runtime |
|---|---|---|
| 1 · Reason line + badges | Structured synthesis output; card template binds new fields | **Today** — ~25 output tok/property, no extra call |
| 2 · Adaptive shape | Agent picks single / shortlist / grouped | **Today** — a field in the same object |
| 3 · Why-not list | Keep near-miss rows the search already returned | **Today** — ~15 tok per rejected row |
| 4 · Known / not-checked | Field-presence map per property | **Today** — pure code |
| 5 · Panel reads agent state | Recommendation object + a state channel to the browser | **Today** |
| 6 · Pin / dismiss / drop-a-chip | Three typed messages back to the agent | **Today** |
| 7 · Expand in place | UI work; detail page stays for deep links + SEO | **Today** |
| 8 · Live progress line | Render the plan the tiered loop already produces | **Today** |
| 9 · Shelf survives sessions | Durable per-user agent state | Deep Agents |
| 10 · Self-enriching cards | One background sub-agent per kept property | Deep Agents |
| 11 · Check running on a card | Human-in-the-loop pause for the OTP | Deep Agents |

**Start with 1.** Structured synthesis output plus a reason line is one
response-format change and one template change — the difference between
"here are 12 results" and "here's why these three."

Steps 9–11 are the same three deepagents features already chosen for the check
agent — durable state, sub-agents, human-in-the-loop — **reused here rather
than bought twice**.
