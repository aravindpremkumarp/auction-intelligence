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

5. **A follow-up could only refer back to properties.** Turn 1 answered
   "areas in Chennai with land/plots under ₹50L" with a nine-row table of
   area names. Turn 2 asked *"which of these areas is growing fast?"* and got
   back *"I don't have enough context… I need to know which specific areas
   you're asking about"* — the agent asking the user to restate a list it had
   written seconds earlier.

   The scope carried filters, `total_count`, and `last_ids`. All three are
   about **auctions**. `last_ids` is what "these"/"those"/"the cheapest one"
   resolves against, and it works — but "these **areas**" is a reference to
   the *names in the previous answer*, and nothing in the scope held them. The
   `group_by` path makes it sharper: in that mode `search_auctions` returns no
   rows at all, so after an area breakdown even `last_ids` is empty.

   The fix carries two more bounded fields on the same scope object — the
   previous question verbatim (capped, one turn) and `last_entities`, the
   `{dimension: [name, …]}` the turn put on screen, harvested from `group_by`
   buckets and from result rows. Deliberately **not** a transcript: it is one
   turn's referents, capped per dimension, and — unlike the filters beside it
   — never merged into tool kwargs. `sanitize_question` / `sanitize_entities`
   re-validate both, because they are client-echoed and land in a prompt.

   A topic **reset** now clears the ids and the names as well as the filters.
   Leaving them was the carried-city bug displaced by one turn: "these" would
   resolve against a subject the user had already dropped.

6. **"Growing fast" was answered as a missing referent, not a missing
   capability.** Even with the areas resolved, the honest answer is that the
   graph holds *current listings* — there is no time series to measure change
   from, which `SCOPE_BOUNDARY` already covers as "no market valuations". The
   planner and synthesizer prompts now name trend/appreciation/growth/demand
   questions explicitly: say plainly what is not held, then answer the closest
   question that is (listing counts per area), and never present a count as a
   trend.

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

---

# Phase 2 — the golden catalogue (20 Aug 2026)

68 questions, scored by the same four evaluators plus the LLM judge that gate
`/chat` nightly. `EVAL_AGENT` selects the agent; nothing else changes between
runs, which is what makes them comparable.

## Head to head, same questions, same afternoon

| | v1 (production) | v2 (tiered loop) |
|---|---|---|
| Primary pass | 59/**65** = 90.8 % | **64/68 = 94.1 %** |
| Citations (report-only) | 22/29 = 75.9 % | **28/30 = 93.3 %** |
| Judge quality, mean | 0.981 | 0.982 |
| Turn median | 119.4 s | **17.9 s** |
| Turn p90 | 266 s | **36.3 s** |
| Full sweep | 47.7 min | **6.8 min** |

**v1's median is ~2 minutes per question** — materially worse than the 73 s
this document quoted from older Logfire traces. That 73 s figure was wrong,
and it is worth saying so plainly: it also underpinned the token estimate
below, which is why that estimate has been withdrawn rather than repeated.

**Run-to-run variance is ±2 cases** on identical code (two v2 runs of the same
build scored 59/68 and 61/68). A single-case difference is noise.

## Case by case — the comparison that decides a migration

**v2 fixes four questions v1 gets wrong**, all analytical: total auction count,
95th-percentile reserve price, and both "borrowers with multiple properties"
variants. Those are precisely the tier-3 Cypher cases the loop was built for.

**Only two failures are v2-only:**

- *Cities in both residential and commercial* — v2 answers it **correctly**,
  via two parallel `group_by` searches and a set intersection, naming the three
  cities with counts. The catalogue accepts only `run_cypher`. Eval strictness,
  not a defect; fixing it is a catalogue change and belongs in its own commit.
- *Litigation against borrower XYZ* — a genuine refusal miss, and the one item
  here that reads as a real blocker.

**Two fail on both agents:** market valuations (the rule conflict below) and
one schema question.

**Two caveats that cut against v2's headline number:**

- v1 scored **65 of 68** — three cases produced no result at all, including
  "which banks have the highest median reserve price?", which v2 answers. So
  v1's 90.8 % sits on an easier denominator than v2's 94.1 %.
- v2 has a single **798 s outlier** dragging its mean to 32.5 s against a
  17.9 s median. Worth chasing before the flag flips.

## What the eval found in v2 (all fixed)

Three real bugs, none reachable by the offline tests. Two would have been
invisible from the tool-trajectory assertions alone — it took reading the
answers, and in one case the judge score.

**1 · v2 shipped without the policy rules.** Its prompts read the domain brief
from `modes/_shared.md`, which v1 also reads, so schema and enums were shared.
The *policy* — what may be claimed, what is off-limits — lived only in
`api/agent.py::_ROLE_PROMPT`. v2 never saw it and failed refusal cases v1
passes. Now `api/policy.py`, read by both; `_ROLE_PROMPT` is composed from the
same constants and is byte-identical, which matters because it is the
cache-keyed prefix of every v1 call.

**2 · The planner narrated instead of planning.** Asked about RBI guidelines it
returned, as the user's answer: *"The user is asking about RBI guidelines…
I'll use `internet_search` to find the relevant circulars."* It then called
nothing. **Every assertion passed**; the judge caught it at 0.2. The contract
never said `direct_answer` was user-facing text. It does now, with a narrow
guard underneath that routes narration to synthesis instead.

**3 · Schema questions answered from a snapshot.** Correct today, read off a
static brief — a new asset category would produce a confidently outdated
answer with nothing to catch it. Now routed to `describe_schema`.

Resolved by inspection with no change: *"Residential auctions in Mumbai"*
failed one run and passed the next, answering well (zero results, coverage is
Tamil Nadu, 499 available if the city filter drops). Variance.

## Open — a product decision, not a bug

**Market valuations.** Asked for the market value of Anna Nagar properties, v2
searched the graph, found nothing current, said so, then used `internet_search`
to produce a ₹/sq-ft table. The rules pull both ways: rule 4 forbids market
valuations, the web-search rule allows market context. The answer is careful —
it states reserve prices are starting bids, not market values — but it is still
a valuation. **Which rule wins is a decision to make.**

## Cost is now instrumented, and not yet measured

The eval recorded **no token data**: `ChatTaskOutput` had no usage field and
`grep -ic token` over a completed run returned 0. Both agents already computed
usage per turn (`TurnResult` on v2, `_usage_fields` on v1) and the eval threw
it away.

Both output shapes now carry a `usage` dict, populated in all four bindings
from those existing extractors, with identical key names on both sides and a
cache-hit-rate line in the summary — the one number that says whether a stable
prompt prefix is billed at the cache rate. Cost is reported, never gated.

**No v1/v2 token comparison exists yet.** One `EVAL_AGENT=v1` run produces it.
Measured for v2 on a 3-case live check: 2.00 model calls, 9,818 input tokens
and 1,081 output tokens per case, 34.8 % cached (≈90 % once a multi-turn
conversation stabilises the prefix).

## Two environment findings

- **`OPENROUTER_MODEL` points at a retired model.** The env sets
  `google/gemini-2.0-flash-001`; OpenRouter 404s it ("No endpoints found"). It
  broke the eval judge on every case of the first run. The same variable feeds
  `pipeline/ocr_extract.py` and `pipeline/verify_and_enrich.py`, so if Render
  carries this value the reviewer's ▶ Re-run extraction button is failing in
  production and the nightly golden judge is failing silently. The repo default
  (`google/gemini-2.5-flash`) works — verified. **Needs a Render env change.**
- **The runner was silent for its whole duration**, so a 48-minute run looked
  identical to a hung one. Each case now logs to stderr as it completes.

## Report-only, but with a UI consequence

Two v2 listing answers surfaced ids and never mentioned them. The matches panel
is citation-driven (`api/chat/panel.py::cited_ids`), so such an answer leaves
the panel showing the previous set — the user reads about one thing while
looking at another. v2 is much better than v1 here (93.3 % vs 75.9 %, so
roughly one v1 listing answer in four desyncs the panel), but the fix is
structural: a populated `recommendation.picks` carries `auction_id` per pick
and satisfies this without asking the model to remember.
