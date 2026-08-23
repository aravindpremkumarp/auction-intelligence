# Chat-agent middleware — decided design (Aug 2026)

Decisions from the middleware design session. **Nothing here is built yet** —
this is the spec to implement when the chat agent moves onto the Deep Agents
harness (per the runtime decision: check agent first, chat migrates last,
gated on the golden eval).

Context: the chat agent runs the **tiered loop** (plan once → execute graph
queries in parallel → synthesize once, with a NEED_MORE round for multi-hop
and a Cypher escape hatch for tier 3). Measured in
`experiments/deepagent-chat/`: ~12s and ~2 model calls per turn, versus 73s
and 5.6 calls in the ReAct-shaped production agent.

**9 middleware total: 5 off-the-shelf, 4 custom.**

---

## Off-the-shelf (5) — LangChain, must be passed explicitly

`create_deep_agent` does **not** install any of these. Its defaults cover
agent *capability* (filesystem, subagents, skills, memory, HITL-if-configured,
`TodoListMiddleware`), not failure handling. Pass these via `middleware=[...]`;
they run alongside the defaults.

| Middleware | Config | Guards against |
|---|---|---|
| `ModelRetryMiddleware` | 2 retries, exponential backoff, 5xx/timeout only — never 4xx | Transient provider failure killing a turn |
| `ModelFallbackMiddleware` | flash → pro, one hop, **log loudly on fire** | Provider outage |
| `ModelCallLimitMiddleware` | ~8 calls/turn | Runaway loop |
| `ToolCallLimitMiddleware` | ~10 calls/turn | Runaway tool use |
| `ToolErrorMiddleware` | Return `ValueError`/`TypeError` to the model as error messages; let real bugs raise | A bad tool argument killing the turn (observed: an invalid `aggregate_field` crashed variant A) |

**Caveat on fallback**: a flash→pro hop changes per-turn cost and busts the
DeepSeek prompt-cache prefix for that turn. Emergency path only, alert when
it fires.

---

## Custom (4) — encode the domain, must be written

### 1. `AnswerGate` — `after_model` (synthesis)
Inspect the draft answer before the user sees it:
- **Grounding**: extract every ₹ amount, count, and `auction_id`; assert each
  appears in this turn's tool results. Violation → one repair call naming the
  mismatch; still failing → degrade to raw rows with a plain caveat.
- **Citation**: web-sourced claims carry their source or get cut.
- **Compliance**: legal-adjacent answers get the "not legal advice" footer by
  rule, not by model mood.

> A graph database guarantees the *tool result* is correct. It cannot
> guarantee the model *transcribed* it correctly. The gate checks the
> transcription, not the database — which is why it is still needed with
> Neo4j underneath.

### 2. `ScopeState` (with reset) — `before_model` + `wrap_tool_call` + `after_agent`
Conversation memory as a typed object (active filters, last result ids, last
total_count), not a transcript. Inject into the planner, merge deterministically
into every search call, harvest after the turn.

**Two additions from review, both required:**
- The planner emits an explicit `scope: carry | reset` decision per turn. A
  topic switch ("which bank has the most auctions?" after a Chennai search)
  must RESET, or the carried city silently wrongs the answer.
- Every answer **states its scope** ("In Chennai, under ₹40L: …") so a wrong
  carry is visible instead of silent.

**Eval gap to close**: the narrowing eval covers carry / shrink / anchor. It
does NOT yet cover topic-switch reset. Add that scenario.

### 3. `IntentGate` — `before_agent`
Cheap classifier (regex tier, then small-model tier for borderline) that runs
before any expensive call. Refuses harvesting-shaped requests and enforces
policy boundaries in code.

> Primary purpose is **protecting the people in the data**, not blocking
> competitors — bulk scraping is better caught by quotas. "List every
> defaulter in Coimbatore with addresses" is a single query, well under any
> rate limit, and is the query that turns this into a harassment tool.
>
> Evidence this must live in code, not the prompt: in the golden run,
> enabling web search *softened* a refusal case, because the policy was
> prompt-resident.

### 4. `InjectionEnvelope` — `before_agent`
Chat ingests pasted WhatsApp forwards and broker blurbs by design. Wrap pasted
blocks in an explicit data envelope, cap length, flag instruction-shaped
strings inside them.

---

## Deliberately NOT used — with reasons

| Middleware | Why not |
|---|---|
| `TodoListMiddleware` | **A deepagents default — disable for chat.** The planner's JSON plan already is the todo list; a second fuzzy copy costs a model call on a 12-second turn. Keep it for the check agent, where runs are minutes long and the list is the product surface. |
| `SummarizationMiddleware` / `SummarizationToolMiddleware` | Solves transcript growth; the scope object removes the transcript. Treating a cured disease. |
| `ContextEditingMiddleware` | Same — nothing accumulates to prune. Revisit only if the state model changes. |
| `PIIMiddleware` | Borrower names are a **search feature** (`search_auctions(borrower=...)`) over legally public SARFAESI notices. Redaction breaks the product. **Deliberate skip — recorded here because an auditor will ask.** |
| `HumanInTheLoopMiddleware` | Chat is read-only; nothing to approve. This is the crown jewel of the *check* workflow instead. |
| `LLMToolSelectorMiddleware` | Pays off at 20+ tools; at 4 it is an extra model call that can only lose. Revisit when land-record tools land. |
| `FilesystemMiddleware`, `SubAgentMiddleware`, `SkillsMiddleware`, `MemoryMiddleware` | Built for long-running investigative agents. All four reappear on the check-agent list. |
| `ToolRetryMiddleware` | Retrying a just-failed Neo4j query usually re-fails. Prefer a circuit breaker if this becomes a problem. |
| `ShellToolMiddleware`, `FilesystemFileSearchMiddleware`, `ProviderToolSearchMiddleware`, `RubricMiddleware` | No chat use case. |

## Demoted, not deleted

- **`SemanticCache`** — deferred by decision: *don't cache what you haven't
  measured*. Let telemetry report the near-duplicate rate for anonymous
  questions first. Build if ~30%, drop the idea if ~5%.
- **`LocaleNormalizer`** — money normalization dropped (the model already
  handles "40 lakhs" → 4000000 correctly). Place-name normalization **kept but
  demoted** to a fuzzy-match alias table inside `search_auctions` — the model
  knows Trichy is Tiruchirappalli, but cannot know which of five spellings of
  Kancheepuram *this graph* uses.
- **`TurnAudit`** — collapsed into LangSmith. It already captures traces,
  tokens, latency, tool calls. The custom part is just attaching domain
  metadata to the trace: answer-gate verdict, scope carried/reset, tier used.

---

## Cost model

| Class | Which | Tokens | Latency |
|---|---|---|---|
| Pure code | AnswerGate checks, IntentGate regex tier, InjectionEnvelope, all 4 limit/retry middlewares on good turns | 0 | microseconds |
| Adds prompt text | ScopeState block (~150 tok), envelope stamp (~30 tok) | ~180 input tok/turn | ~0 |
| Adds a model call — **exception path only** | AnswerGate repair, IntentGate borderline classifier, fallback hop | 1 extra call | +8–12s **on that turn only** |

**Normal turn: ~180 extra input tokens, ~0 added latency.** Bad turn: one
extra call. The design rule that keeps it cheap — *the expensive path must be
the exception path*. Measure the gate's fire rate in the spike before
production commits to the p95 budget.
