# Design: User-Selectable Chat Model + Reasoning-Effort Toggle

Branch: claude/chat-spending-calculation-ds68zo
Repo: aravindpremkumarp/auction-intelligence
Status: Implemented

## Problem

The chat agent ran a single fixed model (DeepSeek V4 **Pro**) with a fixed
reasoning effort (`high`) for every user and every turn. Reasoning tokens bill
as output, so Pro + high effort is the dominant per-turn cost — roughly ₹8
(~$0.10) for a chat with ~10 follow-ups. We want to (a) offer a cheaper, faster
model (DeepSeek V4 **Flash**) so most usage costs a fraction of that, and (b)
let users trade depth for speed/cost per conversation — without letting free
users run up the expensive model.

## Decisions

| ID | Decision |
|----|----------|
| M1 | Two logical chat models, `flash` and `pro`, mapped to config-tunable OpenRouter slugs (`OPENROUTER_MODEL_CHAT_FLASH` / `OPENROUTER_MODEL_CHAT`). |
| M2 | **Free and anonymous users are hard-locked to Flash, server-side.** The client toggle is advisory; `resolve_chat_model` forces Flash for any non-paid tier regardless of the request body — same trust model as the durable chat-quota gate. |
| M3 | Paid users may pick either model; default is Pro. An unknown value falls back to the paid default, never 500s. |
| M4 | Reasoning effort is a per-turn toggle open to all tiers (Flash barely reasons, so it's cheap there). Validated against an allowlist; `None`/unknown falls back to the server default (`OPENROUTER_CHAT_REASONING_EFFORT`). |
| M5 | A single agent instance serves both models via `agent.run(..., model=..., model_settings=...)` per-request overrides — no second agent, no rebuild. |
| M6 | `GET /chat/models` returns the tier-aware option list (`locked` flag on Pro for free users) + defaults, so the UI renders the toggles correctly. The server re-enforces regardless. |

## Shape

- **`api/model_selection.py`** (pure, no agent/network): logical-name→slug map,
  tier gating (`resolve_chat_model`, `resolve_reasoning_effort`), and
  `build_model_settings(effort)` which builds the OpenRouter `extra_body`
  (usage accounting + first-party DeepSeek provider pin + optional reasoning).
  Kept separate from `api/agent.py` so the logic is unit-testable without
  building the real agent, and so the router can import it without the agent.
- **`api/agent.py`**: builds `CHAT_MODELS` (one `OpenAIModel` per logical name)
  and `build_chat_run_overrides(name, effort)` → `{model, model_settings}`.
- **`api/chat/router.py`**: `ChatRequest` gains `model` + `reasoning_effort`;
  `_prepare_turn` resolves + gates them and threads the overrides into both
  `/chat` and `/chat/stream`. The chosen model + effort are added to the
  per-turn obs log so cost is greppable per model. New `GET /chat/models`.
- **Web**: model + thinking-effort dropdowns next to the existing mode picker,
  hydrated from `/chat/models`, persisted in `localStorage`, re-hydrated on
  auth change. Locked models render disabled; selections are sent on each turn.

## Cost rationale

Token pricing (DeepSeek list, $/1M tokens — https://api-docs.deepseek.com/quick_start/pricing/):

| Model | Input (cache hit) | Input (cache miss) | Output |
|-------|-------------------|--------------------|--------|
| deepseek-v4-pro   | $0.003625 | $0.435 | $0.87 |
| deepseek-v4-flash | $0.0028   | $0.14  | $0.28 |

Modelling a chat with 10 follow-ups (~11 turns, ~22 LLM calls; ~8K cached
prefix, ~110K uncached conversation history, output incl. reasoning):

- **Pro:** ≈ $0.071 (~₹6) per chat.
- **Flash:** ≈ $0.020 (~₹1.7) per chat — roughly 3.5× cheaper.

A more typical ~5-turn chat is ~₹2.2 (Pro) / ~₹0.6 (Flash). Against the ₹499 /
30-day unlock, even a Pro power user (~100 chats/mo ≈ ₹220, or ~₹600 if every
chat is a heavy 10-follow-up session) stays profitable except at the extreme
tail; free users (Flash-locked) cost cents. Notably ~68% of a Pro chat's cost
is the **uncached conversation history** re-sent each call — not the cached
prefix or the output — which is why `_trim_old_tool_results` (history trimming
in `api/chat/router.py`) is load-bearing for cost. The reasoning-effort toggle
is the secondary lever: reasoning tokens bill as output, so dropping
`high`→`off` cuts the output term substantially.

Both models sit well under the `OPENROUTER_CHAT_PROVIDER_MAX_PRICE` cap
(`0.9,1.8` $/M prompt,completion), so the first-party DeepSeek provider pin
works for Flash and Pro with no config change.
