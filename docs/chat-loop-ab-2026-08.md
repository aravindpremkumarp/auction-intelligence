# The chat loop A/B — tiered vs Deep Agents (Aug 2026)

Two loops now answer chat turns behind the /lab flag, over the same tools, the
same policy and the same quota:

| | `/chat/v2` — **tiered** | `/chat/deep` — **deep agents** |
|---|---|---|
| Loop | plan → parallel execute → synthesize | ReAct: think → tool → think |
| Memory | a `scope` **summary**, round-tripped by the client | the **transcript**, checkpointed server-side in Neo4j |
| State owner | the browser | the server |
| Model calls/turn | 2.15 measured | ~4–9 expected (spike: 4.5 avg, 9 worst) |
| Tools | `api/chat/v2/tools.py` | the same module, imported |
| Policy | `api/policy.py::SHARED_POLICY` | the same constant, imported |

Pick one with `?loop=deep` on /lab, or the picker in the inspector header.

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

- **Server-owned state fixed a real client bug.** `apiChatScope` was cleared at
  none of the four sites that cleared `apiMessageHistory`, so a new chat
  carried the previous thread's filters into its first question.
  `resetAgentState()` in `web/app.js` is now the single owner, and the deep
  loop's `DELETE /chat/deep/{thread}` makes the server forget too — a class of
  bug that does not exist when the server owns the state.

## Not done

- **Streaming is one `delta`.** Same as the tiered loop today; token-by-token
  is a separate piece of work on both.
- **Subagents are off** (`subagents=[]`). They are the reason `deepagents` was
  added to the lock in the first place, and they belong with the pre-bid check
  workflow, not with a loop comparison.
- **No production traffic.** Both surfaces are admin-only. The A/B is run
  deliberately, not sampled.
- **The live numbers are not in this document yet.** Running
  `evals/run_loop_ab.py` against the live graph is the next step, and the
  results table belongs here when it exists — not a prediction of it.
