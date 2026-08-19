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

## Scope notes

- `internet_search` is wired in only when `TAVILY_API_KEY` is set; the smoke
  questions avoid it so the comparison stays graph-only.
- This spike does not run the golden eval (`evals/`) — that gate applies
  before any production migration, not to the spike.
- Nothing here touches production code paths; the experiment is additive.
