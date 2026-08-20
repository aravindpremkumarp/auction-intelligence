# Chat agent v2 — Phase 1 build notes (Aug 2026)

`/chat/v2` runs the **tiered loop**: one planning call emits every graph query
the question needs, code runs them against Neo4j in parallel, one synthesis
call writes the answer. Escape hatches for a follow-up round (tier 2) and a
composed read-only Cypher (tier 3).

`/chat` and `/chat/stream` are unchanged and stay on pydantic-ai. The flag is
**off by default** — `?chatv2=1` or `localStorage.chat_v2='1'`.

Companion to `docs/chat-agent-middleware-2026-08.md` (which middleware, and
why) and `docs/recommendation-display-2026-08.md` (what a card shows).

---

## What v2 changes

| | v1 | v2 |
|---|---|---|
| Loop | ReAct — think, call a tool, think again | plan → parallel execute → synthesize |
| Conversation state | full `message_history`, re-billed every turn | a small typed `scope` object |
| Tool execution | one at a time, model-gated | concurrent, on a private thread pool |
| Answer | markdown prose | typed object: answer + recommendation |
| Follow-up round | implicit in the loop | an explicit `need_more` field |

Everything else is deliberately identical — the SSE event vocabulary, the
artifact shape, the quota, the panel-sync function, and the eval assertions.

---

## Live results (4-turn narrowing conversation, DeepSeek V4 Flash, 20 Aug 2026)

| turn | question | s | tier | model calls | in tok (cached) | total_count |
|---|---|---|---|---|---|---|
| 1 | residential properties in Tamil Nadu | 21.4 | 1 | 2 | 12,672 (7,168) | 501 |
| 2 | only in Chennai | 20.9 | 1 | 2 | 12,739 (12,288) | 66 |
| 3 | under 40 lakhs | 18.0 | 1 | 2 | 11,665 (11,264) | 20 |
| 4 | which of those had a failed earlier auction? | 62.4 | 2 | 4 | 30,599 (27,392) | 0 |

Two model calls per turn on the ordinary path, against v1's measured 5.6. The
scope carries correctly across all four turns and the count only ever shrinks.
Prompt-cache hit rate climbs to ~90% by turn three, which is what the stable
prefix (shared context + tool catalogue) is for.

**Honest caveat:** per-turn wall clock here (18–21 s) is above the spike's
11.2 s average. Two causes, both real: the prompt now carries the full
`modes/_shared.md` domain brief rather than the spike's slim instructions, and
the synthesizer writes markdown tables (1,000–1,600 output tokens per turn).
Neither is a loop-shape regression. Reducing them is Phase 3 tuning work with
the eval as the guardrail, not something to guess at now.

---

## Bugs the live run found

A staged rollout is only worth having if you actually run it. Four problems
surfaced on the first real conversation, none of which the offline tests could
have caught:

1. **Planner prose shipped as the answer.** After a `need_more` round, the
   planner decided no further calls were needed and its `direct_answer` — text
   written for the loop, not the user — went straight to the screen: *"No
   additional calls needed — the get_auction_detail results already contain
   price_history."* A follow-up round now always falls through to a final
   synthesis.
2. **`get_auction_detail` truncated silently.** The model asked for 15 ids,
   the tool caps at 10, and the answer confidently covered "all 15
   properties". The tool now returns `not_fetched_ids` and says so.
3. **The answer gate flagged the user's own filter.** "under ₹40 Lakhs" is the
   threshold the user just asked for, not a fabricated number. The active
   scope's numeric values now count as grounded.
4. **The answer gate flagged carried auction_ids.** A follow-up question
   legitimately refers back to ids the *previous* turn surfaced; the gate only
   saw the current turn's results. The anchor ids now count as grounded.

## Answer-gate fire rate — the measurement, not a verdict

The gate is **report-only** by design: the rule that keeps this cheap is that
the expensive path must be the exception path, and the fire rate had to be
measured before anything was built on top of it.

After fixes 3 and 4, it fires on **2 of 4 turns**, and both remaining fires
are the same shape: **price-band labels in summary tables** — "Under ₹30L: 45
properties", "₹30L–₹60L", "above ₹1Cr". Those boundaries are a categorisation
the model chose, not a claim about a specific property.

Deliberately **not** tuned away. Suppressing round numbers would also hide a
genuine "₹40,00,000" that drifted a digit, which is the whole point of the
gate. The decision for Phase 2 is whether to teach the gate about band labels
specifically or to accept the rate — with real traffic behind it, not four
turns.

---

## Things worth knowing before working on this

- **The v2 stack is imported lazily**, inside the `/chat/v2` handlers.
  Measured: `import api.main` is 226 MB; the LangChain stack adds 28 MB and
  `deepagents` another 107 MB on top. The Render instance is a 512 MB starter.
  A module-scope import would spend that on every v1-only deploy.
  `tests/api/test_chat_v2_router.py` pins this in a clean subprocess.
- **`deepagents` is installed but never imported.** The tiered loop does not
  use it — deepagents' own loop is the ReAct shape v1 already has. It arrives
  with the subagent work.
- **`Synthesis.answer` must stay the first schema field.** Structured output
  arrives as one JSON blob; streaming the characters inside that first string
  value is what will keep token-by-token output alive. Reordering breaks it.
- **The scope object is a trust boundary.** It is merged into
  `search_auctions` kwargs by code, so `sanitize_scope` runs on every request.
  v1's `message_history` was inert prose by comparison.
- **Three surfaces have one owner each**: scope keys in
  `api/chat/scope_keys.py`, quota + model gating in `api/chat/gating.py`,
  Cypher rules in `cypher_tools.CYPHER_PATTERN_RULES`.

## Not done in Phase 1

- **Token-by-token streaming.** The answer currently ships as one `delta`.
  The incremental JSON scanner that streams the `answer` field is next.
- **Answer-gate enforcement** — repair call and degrade path. Needs the fire
  rate from real traffic first.
- **IntentGate's small-model tier** for borderline phrasing. The regex tier
  ships; an extra model call on every turn is the cost this design avoids.
- **`deep-research`** stays on v1 (v2 returns 400 for it).
- **The property-detail chat** stays on v1 — it threads `message_history`,
  which v2 neither accepts nor returns.
- **Recommendation-display steps 2–11** — adaptive shape, why-not list,
  pin/dismiss, self-enriching cards.

## Running the parity gate (Phase 2)

```bash
EVAL_AGENT=v2 python -m evals.run_golden          # 68 cases, same evaluators
EVAL_AGENT=v2 python -m evals.run_conversations   # 8 conversations
EVAL_AGENT=v1 ...                                 # the baseline to compare against
```

Both agents are scored by identical cases and evaluators — that is what makes
the two runs comparable at all.
