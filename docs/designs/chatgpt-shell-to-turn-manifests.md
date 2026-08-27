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

`TurnManifest` does not exist anywhere in `api/`. No backend work has
started.

## Conformance

| #404 requires | #417 today | Gap |
| --- | --- | --- |
| Cards render inside the assistant message that produced them | Yes — `_inlineMatchesHtml` in `.bubble-wrap.ai` | none |
| Which rows exist comes from the sink, never from parsing prose | Comes from `m.artifacts` via `extractResultsFromArtifacts` | Right source, wrong side of the wire. Not prose-parsing, but re-derived per render on the client |
| Each card carries the agent's own sentence(s) about that property | Facts only — inline cards pass `withReason: false` | **The recommendation is missing.** This is #404's Premise 2 and the reason the whole design exists |
| Panel state is part of the thread; reload restores every turn's cards | Client-saved history only; no `/manifests`, no server `/history` | Reload fidelity is whatever the browser copy holds |
| Scope badge on multi-lot notices | Not rendered | Card can state a lot fact as a property fact |
| Collapsed "all N matches" with `query_echo` and the count delta ("21 → 6") | A "N matches" chip that opens a drawer | Different affordance; no query echo, no delta, no "showing 500 of 812" |
| Empty result renders a "0 matches for {query}" card | Renders nothing inline | "An empty state is a feature" — we have no state |
| `kind: distribution` renders the breakdown table | Not modelled | A `group_by` turn reads as "no matches" |

Two things that are **already right** and should not be disturbed:

- `linkifyAnswerHtml` takes its linkable ids from `collectAuctionIds(m.artifacts)`
  and only uses the prose as the render target. That is #404's layering rule,
  not a violation of it.
- `panelSnapshotIndex` / `showMatchSnapshot` is a client-side per-turn
  snapshot already. It is the ancestor of the manifest and the natural thing
  for the manifest to replace.

## The path

Four stages. Each is shippable on its own and leaves `/lab` working.

### Stage 0 — land #417 as-is

It is green, scoped to `/lab`, and the public site is untouched. Landing it
now gets the layout in front of real turns, which is what the visual half
was for. Nothing below is blocked by it.

Add to the PR body: this doc, and the fact that inline cards are
presentation-only until Stage 2.

### Stage 1 — backend: the manifest (no UI change)

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

### Stage 2 — frontend: cards read the manifest

The change is narrow and lands entirely inside what #417 already built.

- `_inlineMatchesHtml(snap)` becomes `_inlineMatchesHtml(manifest)`. It
  reads `card_rows` + `annotations` instead of `_msgMatches(m)`.
- Each card renders its `annotations[id]` as the reason line. The plumbing
  exists: `propCardHtml(c, urgent, countdown, withReason)` and `_pickHtml`
  already render a reason and badges from `currentPicks`.
- The stream handler consumes the `manifest` SSE event; thread-open calls
  `/history` + `/manifests` and joins on `turn_index`. A message with no
  matching manifest renders card-less — ordinals never shift to compensate.

**Do not flip `withReason` to `true` before this stage.** `currentPicks` is
global and replaced every turn, so scrolled-up answers would show the
*latest* turn's reasons. That mis-attribution is the exact defect #404
exists to kill, and it would look like the feature working. `false` is
correct until reasons are per-turn.

### Stage 3 — the rest of #404's card

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
