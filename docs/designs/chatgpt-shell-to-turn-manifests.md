# Convergence: the ChatGPT shell (#417) onto turn-owned cards (#404)

Written 2026-08-27, after #417 shipped inline property cards on `/lab` and
we noticed #404 had already specified them.

- **#404** — `docs/designs/turn-owned-property-cards.md`. Merged 2026-08-23,
  status APPROVED, **docs only**. Chose "Approach B — cards in the chat
  stream" over a smarter side panel.
- **#389** — `docs/recommendation-display-2026-08.md`. Merged 2026-08-20,
  docs only. The earlier card-and-panel design.
- **#417** — this branch. The ChatGPT shell on `/lab`, including up to four
  inline property cards per answer.

## The verdict in one line

#417 built the *visual* half of the thing #404 approved, and #404 explicitly
deferred that half — its Open Question 2 says "Card visual design — density,
thumbnails, mobile layout — is deliberately out of scope; it goes to
/plan-design-review after this doc." So the two are compatible in intent.
They are not yet compatible in mechanism: **#417 renders cards from
client-side artifacts; #404 requires they come from a server-side per-turn
manifest.** Nothing in #417 has to be thrown away to get there, but three of
#404's four success criteria fail today.

*Written when `TurnManifest` did not exist anywhere in `api/`. Stages 0–2
below have since shipped on this branch; the table records where each
requirement now stands.*

## Conformance

| #404 requires | Status | Note |
| --- | --- | --- |
| Cards render inside the assistant message that produced them | done | `_inlineMatchesHtml` in `.bubble-wrap.ai` |
| Which rows exist comes from the sink, never from parsing prose | done | `card_rows` is written server-side from the sink; the client no longer re-derives them when a manifest is present |
| Each card carries the agent's own sentence(s) about that property | done | `annotations`, quoted verbatim off the answer — #404's Premise 2 and the reason the design exists |
| Panel state is part of the thread; reload restores every turn's cards | done | `/manifests` + `/history`, joined on `turn_index` |
| Scope badge on multi-lot notices | **open** | `notice_lot_count` and `area_sqft_scope` are in `card_rows`; nothing renders them yet, so a card can still state a lot fact as a property fact |
| Collapsed "all N matches" with `query_echo` and the count delta ("21 → 6") | **open** | The chip and drawer stand in for it. `query_echo` and `counts` are stored and unused |
| Empty result renders a "0 matches for {query}" card | **open** | `kind: "empty"` is recorded; nothing renders it |
| `kind: distribution` renders the breakdown table | **open** | Recorded and unused — a `group_by` turn still reads as no matches |

Two things that are **already right** and should not be disturbed:

- `linkifyAnswerHtml` takes its linkable ids from `collectAuctionIds(m.artifacts)`
  and only uses the prose as the render target. That is #404's layering rule,
  not a violation of it.
- `panelSnapshotIndex` / `showMatchSnapshot` is a client-side per-turn
  snapshot already. It is the ancestor of the manifest and the natural thing
  for the manifest to replace.

## The path

Four stages. Each is shippable on its own and leaves `/lab` working.

### Stage 0 — land #417 as-is — **done, on this branch**

It is green, scoped to `/lab`, and the public site is untouched. Landing it
now gets the layout in front of real turns, which is what the visual half
was for. Nothing below is blocked by it.

Add to the PR body: this doc, and the fact that inline cards are
presentation-only until Stage 2.

### Stage 1 — backend: the manifest (no UI change) — **done**

The dependency list #404 already wrote, against the real code:

- `api/agent3/common.py::ToolSink` gains `total` (true match count from the
  aggregation), `query_args` (the last search's arguments), and `breakdown`
  (the `group_by` table, which today reaches only the model's tool message).
  Also absorb on the zero-result and `group_by` paths of `find_properties`,
  which skip the sink entirely today.
- Export the guarded id extraction from `api/agent3/gates.py` as a shared
  helper. `artifacts.cited_ids` is the unguarded variant and is **not** it.
- `api/agent3/loop.py::run_turn` returns a `TurnManifest` alongside
  `TurnResult`. `turn_index` is computed once, server-side, as the count of
  *final* assistant messages (the `AnswerGate.after_model` finality test),
  1-based and including the answer it describes.
- `api/agent3/router.py` persists it before responding (best-effort — a
  persistence failure must not fail the turn), returns it as a `manifest`
  field on `POST /chat/agent3`, and emits a `manifest` SSE event immediately
  before `final` on `/stream`.
- New `GET /chat/agent3/{thread_id}/manifests` and
  `GET /chat/agent3/{thread_id}/history`. `DELETE` already clears the
  thread; it clears manifests in the same call.

`artifacts` stays on the response untouched through the whole migration, so
`/` and the current panel keep working.

Annotation extraction is the deterministic sentence split #404 specifies —
no model call, zero added latency, verbatim quotes. Its known imperfection
(`"Rs. 50 lakh"` clips the quote) is documented there as accepted.

### Stage 2 — frontend: cards read the manifest — **done**

- `_inlineMatchesHtml(m, snap)` reads `m.manifest.card_rows` when there is
  one and falls back to the artifact-derived `snap` for the tiered and deep
  loops and for conversations saved before manifests existed.
- Each card renders its `annotations[id]` as the reason line, and the
  properties the answer discussed sort to the front.
- The cards are a horizontal rail of portrait tiles rather than a vertical
  stack. Stacked full-width rows pushed the next question off screen, which
  is what forced the four-card cap #404 does not have; a rail costs one
  card's height whatever the count, so the cap rose to twelve. Not squares:
  the quote needs three lines at tile width, and a square spends its height
  on whitespace above them.
- `propCardHtml` grew a fifth argument, `reason`, rather than switching
  `withReason` on. That was the trap: `_pickHtml` reads the global
  `currentPicks`, which the newest turn replaces, so a scrolled-up answer
  rendered through it quotes the wrong turn. A string passed down from the
  turn's own manifest cannot make that mistake.
- The stream handler consumes the `manifest` SSE event; reopening a saved
  chat re-fetches `/manifests` and joins by ordinal, and refuses the join
  when the two sides disagree on how many answers there were (a failed turn
  leaves a local-only message, and a shifted ordinal shows a turn another
  turn's properties).
- `_search_artifact` now reports the search's exact `total_count` instead of
  `len(rows)`, so the chip and the panel agree. That was a real defect on its
  own: an 812-match search displayed "500 matches" — the cap, presented as
  the answer.

### Stage 3 — the rest of #404's card — **next**

Once cards are manifest-driven these are each small and independent:
scope badge for multi-lot notices; the collapsed "all N matches" section
with `query_echo` in words and the "21 → 6" delta; "showing 500 of 812" at
the `PANEL_ROW_CAP`; the `empty` and `distribution` kinds.

At that point the matches drawer #417 added has no job left — the collapsed
section inside each turn replaces it. Removing it is the signal that
convergence is done, and it is the moment to revisit #404's Open Question 1
(does the tiered v1/v2 surface adopt manifests, or stay on `panel_sync_ids`
until retired — the doc's own recommendation is agent3-only).

## Success criteria

Carried over from #404 verbatim, because they are the test of convergence,
not of #417:

- Reloading a thread reproduces every turn's cards exactly.
- No display-path code parses prose at render time — verified by rendering a
  stored manifest *after deleting the thread's message history*: cards and
  annotations must both survive, because both live in the manifest.
  (`linkifyAnswerHtml` needs a decision here: it is artifact-gated today,
  which satisfies the layering rule but not this literal test.)
- Each message's cards derive solely from its own manifest.
- A multi-lot notice's card shows its scope badge.

## Open questions

1. **`INLINE_MATCHES_MAX = 4`.** #417 picked four; #404 says "⭐ discussed"
   cards plus a collapsed all-matches section, i.e. the count is *whatever
   the agent discussed*, not a cap. Under #404 the cap disappears —
   `discussed_ids` decides. Worth confirming that is what you want, because
   a turn that discusses nine properties then shows nine cards.
2. **Does the drawer survive Stage 3**, or is the collapsed in-turn section
   the only home for the long tail? #404 implies the latter.
3. **#404's assignment is still open**: take a 7-turn transcript and write
   down, per turn, which cards *should* have shown. That paper trace is the
   acceptance fixture for the Stage 1 manifest tests.
