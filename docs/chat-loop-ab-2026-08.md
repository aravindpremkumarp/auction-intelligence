# The chat loop A/B — tiered vs Deep Agents (Aug 2026)

Two loops now answer chat turns behind the /lab flag, over the same tools, the
same policy and the same quota:

| | `/chat/v2` — **tiered** | `/chat/deep` — **deep agents** |
|---|---|---|
| Loop | plan → parallel execute → synthesize | ReAct: think → tool → think |
| Memory | a `scope` **summary**, round-tripped by the client | the **transcript**, checkpointed server-side in Neo4j |
| State owner | the browser | the server |
| Model calls/turn | 2.00 measured | 1.95 measured (the spike expected 4–9) |
| Tools | `api/chat/v2/tools.py` (4) | the same module, imported, **plus 9 harness tools** |
| Policy | `api/policy.py::SHARED_POLICY` | the same constant, imported |
| `deep-research` mode | rejected (400) | handled by the `property-dossier` subagent |

**The deep loop is the default** on the flagged surface. `?loop=tiered` on
/lab, or the picker in the inspector header, switches back.

Both endpoints stay admin-only, so this decides which loop an *admin* gets —
not what a signed-in user gets. **The A/B has now been run** (conversation
suite, below) and it says the deep loop does not go to users: correctness is a
tie, but at a 149 s median turn most of its passing answers would never have
reached a browser.

---

## Why this is being reopened

The tiered loop was chosen on a real measurement and it won convincingly:
**11.2 s and 2.15 model calls per turn against v1's 73 s and 5.6** across the
68-case golden catalogue. Nothing here disputes that.

What reopened it is a bug. A turn answered *"which areas in Chennai have
land/plots under ₹50L"* with a nine-row table of area names. The next question
was *"which of these areas is growing fast?"*, and the answer was:

> I don't have enough context to answer which area is growing fast. Your
> question refers to "these areas," but I need to know which specific areas
> you're asking about.

The agent asked the user to restate a list it had written seconds earlier.

That is not a prompt bug, and the fix that shipped for it — carrying
`last_question` and `last_entities` on the scope — is not a general answer
either. It is the structural consequence of the memory model:

> **A summary can only answer questions about the things it chose to
> summarise. A transcript can answer any question about anything that was
> said.**

The scope carried filters, `total_count` and `last_ids` — all three about
*auctions*. "These **areas**" refers to the names in the previous *answer*, and
nothing held them. Each fix of this shape adds another field, and the next
question that refers to something no field holds fails the same way. The
question worth answering with data is whether the latency and cost win is
worth continuing to pay for that.

## What is NOT the argument

- **That the spike was wrong.** It measured a vanilla harness with slim
  instructions on a 4-question smoke set, and it was right about what it
  measured. It just is not a basis for the decision two phases later.
- **That "growing fast" would have worked.** It would not have, in either
  loop. The graph holds current listings and no time series, so the honest
  answer names that gap. Both loops' prompts now say so explicitly. The bug
  was the missing referent, not the missing capability, and they are separate
  fixes.
- **That the transcript is free.** It is the thing the tiered loop was built to
  avoid. Every earlier message is re-sent and re-billed on every turn, and the
  prompt cache softens that without removing it.

---

## Running the comparison

```bash
# needs OPENROUTER_CHAT_API_KEY (or OPENROUTER_API_KEY) and NEO4J_*
python -m evals.run_loop_ab                       # both loops, both suites
python -m evals.run_loop_ab --suite convo         # the multi-turn half
python -m evals.run_loop_ab --loop deep --limit 5 # a quick look
python -m evals.run_loop_ab --json runs/ab.json
```

Two suites, because they answer different questions:

- **golden** — 68 single-turn cases, scored on tool trajectory and graceful
  refusal. This is the regression gate: the deep loop has to be *at least as
  correct* per turn before its memory advantage matters at all.
- **convo** — 9 scripted conversations, scored on tool trajectory plus
  `Turn.forbid_answer_markers`. This is where the loops actually differ.

The discriminating case is `refer_to_named_areas` in
`evals/conversations.py` — the screenshot's exact sequence, asserting that a
follow-up referring to names from a previous answer is not bounced back as
"which areas do you mean". Every other conversation in the catalogue refers to
auctions, which is why none of them could have caught the original bug.

## How to read the result

Report all of it; do not reduce it to one number.

- **Correctness first.** If the deep loop loses trajectory or refusal cases,
  stop — a memory advantage does not buy back a wrong answer.
- **Then the conversation suite.** This is what the change is for.
- **Then cost, honestly.** Median and p90 seconds, model calls, input tokens
  and the cached share. The tiered loop's case rests on a stable prefix; the
  deep loop's rests on the provider caching a growing transcript. Both need
  the cached column to mean anything.

A loop that wins on accuracy and loses 5x on cost is a **decision**, not a
verdict. The plausible outcome is neither loop winning outright — tiered for
the common single-turn question, deep for conversations that refer backwards —
in which case the routing rule is the deliverable, not a migration.


---

## The result — conversation suite, 21 Aug 2026

9 scripted conversations, 22 turns, each loop run once against the live
graph. Both loops under the same 300 s ceiling (see the harness note below).

| | tiered | deep |
|---|---|---|
| pass | **21 / 22** | 20 / 22 |
| median turn | **25.4 s** | 148.7 s |
| p90 turn | **58.8 s** | 257.7 s |
| model calls / turn | 2.00 | 1.95 |
| prompt tokens sent | 10,909 | 22,275 |
| — of which cached | 7,343 | 5,364 |
| **fresh prompt (billed)** | **3,566** | 16,911 |
| answer tokens | 1,056 | **677** |

**Correctness is a tie; cost and latency are not.** The deep loop resolves
every referring follow-up in the catalogue, including the one the tiered loop
could not answer before the `last_entities` fix and the harder
`aside_offgraph_then_resume` sequence (ask about Kanchipuram, detour to "what
does EMD mean", then "ok, back to those") — which no summary-shaped state
would survive. It pays about **4.7x the fresh prompt tokens and 6x the
latency** for it.

Its two failures are not memory failures:

- `refine_residential_chennai#4` ran past even the 300 s ceiling. It had
  called `internet_search`, which was broken (below).
- `refine_commercial_coimbatore#3` answered "only the ones on BAANKNET"
  without searching.

The tiered loop's single failure is `refine_residential_chennai#4` too — the
same question, reaching for `get_auction_detail` instead of a search. Neither
loop can answer "which of those close within the next week".

### The decision

**Neither loop wins outright, and the split is not the one predicted.** The
prediction was tiered for single-turn, deep for referring follow-ups. What the
data says is narrower: the deep loop's memory works and its *per-turn* answer
is no worse, but at 149 s median it is not a chat interface — the browser's
idle guard gives up at 75 s, so **more than half of the deep loop's passing
turns would never have reached a user.**

So the deep loop stays on /lab and does not go to users. The tiered loop keeps
production. Before that is worth revisiting, one thing has to change:

**The prompt cache is the whole affordability case, and it is not working.**
The tiered loop gets 67% of its prompt from cache. The deep loop gets 24%, and
tracing showed the expensive call — the one that writes the answer — hitting
**zero** cache on every sample. The transcript is supposed to be cheap on the
second turn onward precisely because its prefix is stable. Until that is
understood, the deep loop's cost number is measuring a broken cache rather
than the cost of remembering.

### What these numbers cannot tell you

**The provider's throughput swings more than the effect being measured.** The
same tiered question was timed at 12 s, 52 s and 93 s on three separate runs
with no code change in between; a controlled generation test gave 3.4 to 9.7
tokens/second across six samples minutes apart. Every figure above is one
sample per case. The pass counts are solid; the latency medians are the right
order of magnitude and no more.

Three hypotheses for the latency gap were tested and **all three were wrong**:

- *It loops.* No — 2 model calls per turn, the same as the tiered loop.
- *Reasoning effort is too high.* No — `low` was **slower** than `high`
  (133 s vs 64 s on the same call).
- *The 9 harness tool schemas slow generation.* No — 51 s with them, 87 s
  without, on the same prompt.

What is left is that the deep loop makes its two calls strictly in sequence
(think, then write) while the tiered loop overlaps its work, on a provider
generating at roughly 8 tokens/second.

### Three harness bugs found while running this

All three flattered or damaged one loop specifically, and all three are fixed
with tests. They are recorded because each one produced a plausible,
publishable, wrong number.

1. **The ceiling was one-sided.** The runner applied the deep loop's 120 s
   `TURN_TIMEOUT_S` to the deep loop only; the tiered loop has no turn-level
   ceiling at all. The first run reported **deep 5/22 with 15 timeouts** — a
   verdict that was mostly the harness. Under one ceiling it is 20/22.
2. **Old turns were billed again.** Deep-loop usage was summed over the
   messages the graph returned, which with a checkpointer is the whole
   conversation — so turn 2 re-charged turn 1, turn 3 re-charged 1+2. Traced:
   49,550 input tokens reported against an actual 29,877. This read exactly
   like "the transcript is getting expensive", which is the claim under test.
3. **A timed-out turn reported zero tokens.** Usage was read off the returned
   messages, and a turn that times out returns none. The most expensive turns
   were being averaged in as free.

### And one product bug

`internet_search` is a coroutine function; the other three tools are not. The
deep loop wrapped all four synchronously, so calling it returned a *coroutine
object* — no error, no result, reaching the model as
`<coroutine object internet_search at 0x...>`. The only symptom was a
`RuntimeWarning` at interpreter shutdown. The model called it three times in a
row on one question getting nothing back each time, and the turn that blew the
300 s ceiling had called it too. Fixed; `_wrap` now preserves each tool's
sync/async nature, with a test that pins every tool's kind against
`PLANNER_TOOLS`.

---

## What was built

| | |
|---|---|
| `api/chat/deep/agent.py` | `create_deep_agent` over the v2 tool surface |
| `api/chat/deep/loop.py` | one turn, returning the v2 `TurnResult` shape |
| `api/chat/deep/checkpointer.py` | `BaseCheckpointSaver` on Neo4j |
| `api/chat/deep/router.py` | `/chat/deep`, `/chat/deep/stream`, `DELETE /chat/deep/{thread}` |
| `evals/run_loop_ab.py` | both loops, both suites, side by side |

### Things worth knowing before working on this

- **The response contract is shared on purpose.** `TurnResult` is
  field-compatible across both loops, so `build_artifacts`, `panel_sync_ids`
  and the whole matches-panel path in `web/app.js` work against either.
  `test_turn_result_is_field_compatible_with_the_tiered_loop` pins it —
  a field on one and not the other turns a loop switch into a 500 on /lab.

- **The call limit is not shared.** `ModelCallLimitMiddleware(run_limit=3)` is
  right for a tier (one call by construction) and wrong for a ReAct turn (the
  spike measured 9). With `exit_behavior="error"` the tier's limit would fail
  every hard question and score the A/B against a loop that was never allowed
  to finish. `model_middleware(run_limit=...)` is now a parameter with a test
  on the difference.

- **`_ui_results` must be stripped before the tool returns.** In the tiered
  loop the executor splits the panel's 500 rows out of the model-visible
  result. Here the return becomes a `ToolMessage` that is checkpointed and
  re-sent every later turn, so an unsplit payload is re-billed for the rest of
  the conversation.

- **The checkpointer stores the serializer's type tag.** `JsonPlusSerializer`
  picks msgpack for most payloads and falls back per value; hardcoding `json`
  on the read side corrupts every checkpoint, and it surfaces as a decode
  error deep inside the graph rather than at the write. Found by a test, not
  in production.

- **Neo4j, not Postgres.** Supabase in this repo is auth only — JWT
  verification against JWKS. There is no SQL connection, no `DATABASE_URL` and
  no driver in the lock file. Conversations already live in the graph, so the
  checkpoint sits next to them; the official Postgres checkpointer would have
  meant a second datastore and a second pool on a 512 MB instance.

- **`deepagents` costs ~107 MB of RSS** on top of LangChain's ~28 MB. Every
  import that reaches it lives inside a handler, and
  `test_importing_the_app_does_not_load_deepagents` pins that in a clean
  subprocess. A module-scope import is a deploy-time OOM, not a test failure.

- **The harness tools are on and cannot be removed — and this document said
  the opposite.** An earlier revision claimed "subagents are off
  (`subagents=[]`)" and that the filesystem middleware was disabled. Both were
  false. `create_deep_agent` adds `FilesystemMiddleware` and the
  general-purpose subagent unconditionally; `subagents=[]` does not suppress
  the `task` tool, and a `HarnessProfile(excluded_tools=...)` registered
  against a pre-built `BaseChatModel` does not drop them from the tool node
  either (verified against 0.7.7). The deep loop's real surface is our four
  graph tools plus `ls`, `read_file`, `write_file`, `edit_file`, `delete`,
  `glob`, `grep`, `execute`, `task`.

  The claim survived because the assertion meant to catch it inspected the
  middleware *we pass in*, never the stack the harness assembles around it.
  `test_the_bound_tool_surface_is_pinned` now asserts on the compiled graph.

- **`execute` is bound but inert, and that is load-bearing for the A/B's
  safety story.** The default backend is `StateBackend`, an in-memory virtual
  filesystem in graph state: no `execute` method, does not satisfy
  `SandboxBackendProtocol`, so the tool returns an error string rather than
  running a command. No shell, no real disk.
  `test_execute_cannot_reach_a_shell` is the tripwire for a version bump that
  swaps that default — on a chat endpoint taking arbitrary user text, a
  sandbox backend would mean shell execution behind a prompt.

### The handicap the cost columns carry

Nine harness tool schemas ride in **every** deep-loop prompt and cannot be
removed. The tiered loop carries four. So the deep loop's input-token column
is not a like-for-like measure of the loop shape — part of the gap is a fixed
tax the library imposes, not a property of keeping a transcript. Read the
token numbers with that subtracted in mind, and do not attribute the whole
difference to memory.

- **Server-owned state fixed a real client bug.** `apiChatScope` was cleared at
  none of the four sites that cleared `apiMessageHistory`, so a new chat
  carried the previous thread's filters into its first question.
  `resetAgentState()` in `web/app.js` is now the single owner, and the deep
  loop's `DELETE /chat/deep/{thread}` makes the server forget too — a class of
  bug that does not exist when the server owns the state.

## Not done

- **Streaming is one `delta`.** Same as the tiered loop today; token-by-token
  is a separate piece of work on both.
- **Subagents are on, with one named worker.** `property-dossier` handles
  `deep-research` — a full due-diligence pass on a single property, and the
  one mode the tiered loop rejects outright. It shares the turn's `ToolSink`,
  so its graph calls reach the matches panel, the answer gate and the eval
  trajectory like any other. More workers (a pre-bid check) are still future
  work; what is settled is that the `task` tool now delegates to something
  with a brief rather than to a blank general-purpose clone of its parent.
- **No production traffic.** Both surfaces are admin-only. The A/B is run
  deliberately, not sampled.
- **The golden suite has not been run.** Only the conversation half below.
  68 cases x 2 loops at these latencies is several hours, and the
  correctness gate it represents is still owed before any un-gating.
