# deepagent-chat — chat-agent spike on the Deep Agents harness

A/B experiment for the chat agent, per the tiered redesign
(see the "Route by difficulty" section of the planning artifact):

- **Variant A** — the vanilla `deepagents` loop (ReAct-style: think → tool →
  think → tool) given the same tools and a slim instructions file. Measures
  what the harness does unaided. Expected to look like production today
  (~5-6 sequential model calls per turn).
- **Variant B** — the tiered loop built on plain LangChain calls:
  one **planning** call emits every graph query the question needs, code
  executes them against Neo4j **in parallel**, one **synthesis** call writes
  the answer. An escape hatch lets the synthesizer request one more planning
  round (tier 2); cap is 2 rounds.

Both variants reuse the production tool implementations from
`api/tools/cypher_tools.py` unchanged — no pydantic-ai import, no FastAPI.

## Why this exists

Production traces (Logfire, 14 turns): a chat turn averages **73 s** —
5.6 sequential model calls at 12.5 s each; the median call carries 6,881
input tokens of which ~86% is static prompt. The hypothesis: the loop shape,
not the database or the model, is the latency. This spike measures that.

## Run it

```bash
# from the repo root, with the repo's prod deps installed:
pip install -r requirements.txt              # repo deps (neo4j, httpx, ...)
pip install -r experiments/deepagent-chat/requirements.txt   # deepagents etc.

# needs in env: OPENROUTER_CHAT_API_KEY (or OPENROUTER_API_KEY),
#               NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD,
#               GOOGLE_API_KEY (only for semantic questions)
# behind an HTTP-only proxy (no Bolt): NEO4J_HTTP_API=1 (set by default here)

python -m experiments.deepagent-chat.run_compare            # smoke set (4 questions)
python -m experiments.deepagent-chat.run_compare --all      # full set
python -m experiments.deepagent-chat.run_compare --variant b --q 1
```

(If `python -m` complains about the dash in the folder name, run
`python experiments/deepagent-chat/run_compare.py` directly — the scripts
fix up `sys.path` themselves.)

## What to look at

Per question and variant: wall-clock seconds, number of model calls,
input/output tokens, and the answer text. The success gate for the tiered
design: **B answers the same questions correctly with 2-3 model calls and
well under half of A's wall clock.**

## Results (smoke set, DeepSeek V4 Flash, reasoning off, live graph, 2026-08-19)

| Q | tier | variant | s | model calls | in tok | out tok | tools |
|---|------|---------|---|-------------|--------|---------|-------|
| 1 | T1 filter | A vanilla | 18.3 | 3 | 14,732 | 916 | search ×2 |
| 1 | T1 filter | **B tiered** | **11.9** | **2** | **1,618** | 187 | search ×1 |
| 2 | T1 aggregate | A vanilla | 10.8 | 3 | 12,870 | 337 | search ×2 |
| 2 | T1 aggregate | **B tiered** | **5.0** | **2** | **1,533** | 141 | search ×1 |
| 4 | T2 multi-hop | A vanilla | 59.5 | 9 | 61,313 | 2,088 | search ×5, semantic ×3, details |
| 4 | T2 multi-hop | **B tiered** | **20.8** | **2** | **1,760** | 325 | search ×2 |
| 5 | off-graph | A vanilla | 12.9 | 3 | 16,525 | 671 | semantic, details |
| 5 | off-graph | **B tiered** | **10.6** | **1** | **961** | 135 | — |

Averages: **A** 25.4 s, 4.5 model calls, 26.4k input tokens per turn.
**B** 12.1 s, 1.75 model calls, 1.5k input tokens per turn.
**B is ~2x faster and ~18x cheaper on input**, with grounded answers on all
four questions (including surfacing the zero-result refine diagnostics).
The multi-hop question is the story: A wandered through 9 model calls and
61k input tokens — the exact production pathology — while B planned twice
and finished in 20.8 s. Answer quality here is eyeballed, not eval-scored;
the golden eval remains the migration gate.

Two harness findings from variant A worth keeping:

1. **deepagents' tool node re-raises tool exceptions**, killing the whole
   turn (an invalid `aggregate_field` crashed the run). Production
   pydantic-ai feeds the error back to the model. The spike wraps tools with
   `_model_visible_errors` to compensate — any migration needs the same.
2. **DeepSeek V4 defaults to reasoning ON via OpenRouter** — without
   production's explicit `{"reasoning": {"enabled": false}}`, variant B's
   planner burned 4.5k thinking tokens per call. `llm.py` now sends the
   same shape production does.

Plus one production bug found on the way: `NEO4J_HTTP_API=1` mode crashes on
any datetime Cypher param (`_http_run` json.dumps's params raw), which every
dated `search_auctions` call hits. The spike shims it via the Query API's
typed-parameter format (`application/vnd.neo4j.query`); a proper fix belongs
in `api/neo4j_client.py`.

## Golden catalogue (all 68 evals/cases.py questions, variant B)

`run_golden_b.py` scores the two production gates — tool trajectory and
graceful refusal — plus latency/tokens:

```
cases 68 | pass 56 | direct 4 | FAIL 8
avg 6.5s | 1.87 model calls | 1,828 input tokens per turn
```

Perfect (all pass): basic_filter 9/9, refusal 5/5, semantic 4/4,
specific_auction 5/5, superlative 4/4, temporal 3/3, reauction 4/4,
borrower 3/3, edge 5/5. The 4 `direct` rows are off-graph/schema questions
answered without tools (no Tavily key in this environment) — informational,
not failures.

**All 8 failures are tier-3 questions** whose expected tool is `run_cypher`
(percentiles, month grouping, borrower-with-N-properties, cross-category
joins) — the escape hatch this spike deliberately did not build. The planner
either honestly said the tools can't express it, or approximated with
`group_by`. Two also tripped over `reserve_price` vs `reserve_price_num`
naming, which argues for enum-typed tool args. Conclusion: the tiered loop
covers 60/64 non-cypher cases cleanly at ~6.5s/turn; tier 3 (deferred Cypher
capability) is required for the remaining 12%, exactly as the design
predicted.

## Golden catalogue + tier 3 (2026-08-19, second pass)

With tier 3 wired in (planner signals `cypher_request` → live schema → one
composed read-only Cypher under production guardrails → one error-feedback
retry), the full 68-case golden catalogue:

| | first pass (no tier 3) | with tier 3 |
|---|---|---|
| pass | 56 | **61** |
| direct (off-graph, informational) | 4 | 6 |
| FAIL | 8 | **1 → 0 after a routing hint** |
| avg per case | — | 9.3 s · 2.06 model calls · 2,598 in-tokens |

All 8 former failures were `run_cypher` cases (per-group aggregates, monthly
volume, p95, borrowers-with->1-property); all now answer through tier 3 in
3 model calls. The one remaining FAIL was an over-route — a re-auction
question sent to Cypher when `search_auctions(is_reauction=true)` already
carries `previous_reserve_price` — fixed with one routing line in
instructions.md and re-verified.

## Narrowing conversations (scope object, not transcript)

`run(question, state=...)` carries a scope object across turns — active
filters, last result ids, last total_count. Code merges the scope into
every `search_auctions` call deterministically; the model only expresses
changes (new value overrides, explicit null drops). `eval_narrowing.py`
scores this programmatically (no LLM judge): **carry** (filters accumulate
into executed args), **shrink** (counts never grow while narrowing),
**anchor** (a final detail turn uses an id from the prior results).

Both scenarios pass end to end:

```
T1 residential properties      → 513
T2 only in Chennai             → 66    carry ✓ shrink ✓
T3 under 40 lakhs              → 20    carry ✓ shrink ✓
T4 only failed earlier sales   → 1     carry ✓ shrink ✓
T5 details of the cheapest one → anchors to T4's id ✓
```

Input tokens stay flat (~2-4k per turn) — turn five costs the same as turn
one, because state is a scope object, not a transcript. Scenario 2 also
covers dropping a filter mid-conversation ("actually any property type is
fine, but only in Coimbatore").

The first narrowing run failed honestly and improved the design: the
planner filtered "Residential" on `property_type` (it belongs on
`asset_category`) and ran the whole conversation on zero rows. Fix: the
enum values moved into the `search_auctions` docstring — schema lives in
the tools, not the prompt.

## Scope notes

- `internet_search` is wired in only when `TAVILY_API_KEY` is set; the smoke
  questions avoid it so the comparison stays graph-only.
- This spike does not run the golden eval (`evals/`) — that gate applies
  before any production migration, not to the spike.
- Nothing here touches production code paths; the experiment is additive.
