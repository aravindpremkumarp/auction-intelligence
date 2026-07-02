"""
api/agent.py
------------
PydanticAI agent wired to OpenRouter (DeepSeek V4 Pro by default — automatic
prompt caching + reasoning; see pipeline/config.py) with Cypher tools.
Keeps the existing OpenRouter config from pipeline/config.py.

The system prompt is assembled from two parts:
1. A short role statement defined here.
2. `modes/_shared.md` — schema, enum lists, tool-choice rules, Cypher
   cheat-sheet. Keeping the schema in markdown lets us edit it without
   touching Python and makes the prompt reviewable in PRs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ToolReturn
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from pipeline.config import (
    OPENROUTER_CHAT_API_KEY,
    OPENROUTER_BASE_URL,
)
from api.model_selection import (
    CHAT_MODEL_SLUGS,
    DEFAULT_PAID_MODEL,
    build_model_settings,
)
from api.alerts import repository as alerts_repo
from api.alerts.service import build_alerts
from api.dossier import dossiers_enabled, qa as dossier_qa
from api.tool_returns import split_ui_overflow
from api.tools import cypher_tools as T
from api.tools import web_tools as W
from api.watchlist import repository as watchlist_repo
from scoring.auction_scorer import score_auction as _score_auction


@dataclass
class ChatDeps:
    # `active_filters` is the rolling scope narrowed across prior turns —
    # every non-aggregate, non-limit arg the user stuck with so far. Injected
    # into the system prompt so the model carries them forward on the next
    # search_auctions call unless the user explicitly changes or drops one.
    active_filters: dict | None = None
    last_total_count: int | None = None
    mode: str | None = None
    # Supabase id of the authenticated caller, or None for anonymous chat.
    # The watch/alerts tools are per-user, so they read this off the deps;
    # plain (context-free) tools don't need it.
    supabase_id: str | None = None
    # auction_ids currently shown in the UI matches panel, in display order.
    # The panel is browser state the model can't otherwise see, so a bare
    # "compare these" / "the matches" had nothing to resolve to. Only the ids
    # ride in context (cheap — a handful of short strings); the model pulls
    # full rows on demand. Injected by `inject_panel_selection`; absent/empty
    # when the panel is empty so the cached prompt prefix is untouched.
    panel_auction_ids: list[str] | None = None


_ROLE_PROMPT = """\
You are the assistant for the Bank Auction Intelligence Platform: help users
find, analyze, score, and track Indian bank-auction properties (mostly
SARFAESI) over a Neo4j knowledge graph of Tamil Nadu properties. The shared
context below holds the schema, enums, tool routing, and Cypher rules; the
live graph size is supplied to you each turn.

Rules:
1. Ground every answer in tool output. Never invent auction_ids, prices,
   counts, enums, or numeric thresholds (`min_price`, `max_price`,
   `starts_after`, `starts_before`); for "cheapest/soonest/top N" use
   `order_by` + `limit`, never a made-up threshold. Cite by `auction_id`.
2. Prefer the specialized tool that matches; fall back to `run_cypher` only
   for novel queries (see Tool routing below). On zero results follow the
   zero-result protocol: read the result's `hint`, spend at most two
   follow-up variations, then answer honestly with what you found.
3. Use `internet_search` only for OFF-graph context (legal/RBI explainers,
   locality background, term definitions) — never for properties, prices,
   deadlines, auction_ids, or counts; for hybrid questions query the graph
   first.
4. Stay on the tool surface. The PUBLIC graph holds AuctionProperty,
   Borrower, Bank, City, Area, Document, AssetCategory — and nothing else. No
   litigations, court cases, FIRs, credit history, ownership chains, market
   valuations, or external records. Frame borrower follow-ups as
   `borrower_lookup` output, never "check legal records". Never offer or
   agree to an action no tool performs — if you can't do it, say so plainly
   and name the closest tool that exists. The ONLY monitoring is
   auction-deadline alerts via `watch_property` / `list_alerts` (their
   docstrings are authoritative); confirm before the state-changing
   `watch_property`.
5. Markdown only for genuine multi-section answers: open each section
   with `### <emoji> **Title**` (one emoji matching intent — 📍 location,
   🔍 search, 🏆 top, 📊 data, 📰 news, ⚡ insight, ⚠️ caveat, ✅, 💰, 📅).
   Separate sections with a blank line + `---` + blank line. Use **bold**
   for load-bearing facts; short bullets for parallel points; real
   Markdown tables (with `|---|`) for tabular data. Don't wrap a short
   single-section reply in headers.
"""

# Appended to the role prompt only when the private-dossier feature is enabled
# (see api.dossier.dossiers_enabled). Kept out of _ROLE_PROMPT so the feature
# ships dark: when it's off the model is never told the tool exists, and the
# always-sent prompt prefix shrinks back to its pre-dossier size.
_DOSSIER_RULE_EXCEPTION = """

6. Private dossier (signed-in users only) — EXCEPTION to rule 4: the user's OWN
   uploaded documents (their private dossier: sale deeds, EC, patta, etc.) ARE
   available via `query_user_dossier`, even though they aren't in the public
   graph. For questions about *their* documents ("what does my EC say…", "is
   there a prior mortgage in my deed", "summarize my dossier"), call it and
   ground the answer in the excerpts it returns — quote the document, never
   invent contents."""

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODES_DIR = _REPO_ROOT / "modes"


def _load_mode_file(name: str) -> str:
    """Read a modes/<name>.md file if it exists; otherwise return ''."""
    path = _MODES_DIR / f"{name}.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


_SHARED_CONTEXT = _load_mode_file("_shared")

_ROLE = (_ROLE_PROMPT + _DOSSIER_RULE_EXCEPTION) if dossiers_enabled() else _ROLE_PROMPT
SYSTEM_PROMPT = f"{_ROLE}\n\n---\n\n{_SHARED_CONTEXT}" if _SHARED_CONTEXT else _ROLE


# Explicit timeout: pydantic-ai's default HTTP client waits far longer, and a
# hung OpenRouter call would pin a worker for the whole request. 90s covers
# slow reasoning turns; connect failures should surface fast.
_http_client = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0))
_provider = OpenAIProvider(
    api_key=OPENROUTER_CHAT_API_KEY, base_url=OPENROUTER_BASE_URL, http_client=_http_client
)

# One model object per logical name ("flash"/"pro"), all on the same OpenRouter
# provider. The chat router resolves a per-request model name (gating free users
# to Flash) and passes the matching object to `agent.run(..., model=...)`, so a
# single agent instance serves both models without rebuilding anything.
CHAT_MODELS: dict[str, OpenAIModel] = {
    name: OpenAIModel(slug, provider=_provider)
    for name, slug in CHAT_MODEL_SLUGS.items()
}

# OpenRouter-specific request extras ride in `model_settings["extra_body"]`,
# built per request by `api.model_selection.build_model_settings`:
#   * usage.include — return detailed usage accounting so the /chat obs log can
#     report prompt-cache hits (`prompt_tokens_details.cached_tokens`) and cost.
#     DeepSeek's context caching is automatic: the stable leading prefix (this
#     role prompt + _shared.md + the tool schemas) is cached upstream and billed
#     at the cache-hit rate on repeat calls, and the cache persists long enough
#     to survive our bursty traffic — so `cached_tokens` should climb above 0.
#     To reduce input cost further, keep that prefix byte-for-byte stable.
#   * reasoning.effort — enable the model's reasoning ("high"/"xhigh" for the Pro
#     tier). User-selectable per turn (the reasoning-effort toggle) and omitted
#     when "off". NB: reasoning tokens bill as output, so this is the biggest
#     per-turn cost lever.
#   * provider — pin routing to first-party DeepSeek so the cache-hit assumption
#     holds and a fallback can't land on the ~3-4x third-party tier.
# The agent's own defaults (Pro + server-default effort) are only used if a
# caller runs it without per-request overrides; the router always supplies them.
agent = Agent(
    CHAT_MODELS[DEFAULT_PAID_MODEL],
    deps_type=ChatDeps,
    system_prompt=SYSTEM_PROMPT,
    model_settings=build_model_settings(None),
)


def build_chat_run_overrides(
    model_name: str | None, reasoning_effort: str | None
) -> dict:
    """Per-request `agent.run` / `agent.run_stream_events` overrides for the
    resolved model + reasoning effort. Returns a dict to splat at the call site:
    `{"model": <OpenAIModel>, "model_settings": {...}}`. An unknown `model_name`
    falls back to the paid default so a bad value can never 500 a turn."""
    model = CHAT_MODELS.get(model_name or "", CHAT_MODELS[DEFAULT_PAID_MODEL])
    return {"model": model, "model_settings": build_model_settings(reasoning_effort)}


# Both inject_* are registered as `@agent.instructions` rather than
# `@agent.system_prompt(dynamic=True)`. Two reasons:
#   1. Cache stability — instructions are skipped when they return "" or
#      None (pydantic-ai's _get_instructions filters falsy strings), so
#      no empty system message rides on the wire when there's no active
#      mode. Dynamic system prompts always emit a SystemPromptPart even
#      for empty output, which polluted Gemini's implicit cache prefix.
#   2. Instructions are not persisted in message history — they're added
#      fresh at request-build time on every turn, so old stored turns
#      can't carry a stale "Active search scope" block when the user
#      narrows scope differently later.
# Old stored histories may still contain dynamic SystemPromptParts that
# referenced these functions; the /chat handler strips them before
# forwarding so they don't linger as orphan refs.
@agent.instructions
def inject_prior_search(ctx: RunContext[ChatDeps]) -> str:
    filters = ctx.deps.active_filters if ctx.deps else None
    total = ctx.deps.last_total_count if ctx.deps else None
    if not filters and total is None:
        return ""
    lines = ["Active search scope (carry on every search_auctions call until the user changes it):"]
    if filters:
        for k, v in filters.items():
            lines.append(f"- {k}: {v!r}")
    else:
        lines.append("- (no scope filters yet)")
    if total is not None:
        lines.append(f"- last total_count: {total}")
    return "\n".join(lines)


@agent.instructions
def inject_panel_selection(ctx: RunContext[ChatDeps]) -> str:
    """Surface the auction_ids the user is currently looking at in the UI
    matches panel, so a bare "compare these" / "the matches" / "all of them"
    resolves to concrete ids instead of a blank search. The panel is browser
    state the model can't otherwise see. Only the ids ride in context (a few
    short strings); the model fetches full rows on demand via
    `get_auction_detail` / `select_properties`. Returns "" when the panel is
    empty so the cached prompt prefix stays byte-stable on those turns."""
    ids = ctx.deps.panel_auction_ids if ctx.deps else None
    if not ids:
        return ""
    shown = ", ".join(ids)
    return (
        f"Matches panel: the user is currently viewing these {len(ids)} "
        f"propert{'y' if len(ids) == 1 else 'ies'} in the UI, in this order: "
        f"{shown}.\n"
        'When the user says "these", "those", "the matches", "all of them", '
        '"the ones above", or similar WITHOUT naming auction_ids, resolve the '
        "reference to this list. To re-rank or show a subset in the panel, call "
        "`select_properties` with the chosen auction_ids. Comparison handles "
        "2-5 at a time: if the user asks to compare more of them than that, do "
        "NOT refuse — ask which 2-5 they want (or offer the top 5) and compare "
        "those."
    )


@agent.instructions
def inject_mode_overlay(ctx: RunContext[ChatDeps]) -> str:
    """If the caller requested a mode (deep-research / compare / report),
    append the mode's markdown spec to the turn instructions."""
    mode = ctx.deps.mode if ctx.deps else None
    if not mode:
        return ""
    text = _load_mode_file(mode)
    if not text:
        return ""
    return f"---\n\n# Active mode: {mode}\n\n{text}"


# Registered as an instruction (not baked into the static role prompt) so the
# graph size reflects the live database rather than a hardcoded number that
# drifts as the loader ingests auctions. It's an instruction rather than a
# dynamic system prompt for the same reasons as the two above: skipped cleanly
# when the count is unavailable, and never frozen into stored history. The
# value is cached in-process, so this costs one cheap query per TTL, not per
# turn.
@agent.instructions
async def inject_graph_size() -> str:
    """Surface the live AuctionProperty total so "how many properties are
    there" answers track the real graph size. Returns "" when the count can't
    be read (cold cache + DB outage); the model then falls back to a search
    tool's `total_count` per Rule 1 instead of inventing a figure."""
    count = await T.graph_property_count_async()
    if count is None:
        return ""
    return (
        f"Knowledge-graph size: it currently holds {count:,} Tamil Nadu "
        'bank-auction properties in total. Use this figure for whole-graph '
        '"how many properties are there" questions. For any filtered subset, '
        "call a search tool and report its `total_count` — never this total."
    )


@agent.tool_plain
def search_auctions(
    min_price: float | None = None, max_price: float | None = None,
    city: str | list[str] | None = None,
    area: str | list[str] | None = None,
    property_type: str | list[str] | None = None,
    asset_category: str | list[str] | None = None,
    bank: str | list[str] | None = None,
    auction_type: str | None = None,
    branch_name: str | None = None,
    starts_after: datetime | None = None, starts_before: datetime | None = None,
    limit: int = 10,
    order_by: str = "deadline_asc",
    aggregate_field: str | None = None,
    aggregations: list[str] | None = None,
    include_past: bool = False,
) -> dict | ToolReturn:
    """Filter auctions by price, city, area, property_type, asset_category,
    bank, auction_type, branch, and date window.

    Returns {total_count, returned, limit, results}: `total_count` is the
    true match count (ignores `limit`); `results` is capped at `limit` and
    never exceeds 25 rows to you (the UI shows every match). Use
    `total_count` for "how many", never `len(results)`. Future-only by
    default; pass `include_past=True` only for retrospective questions. A
    zero-match result may carry `past_matches` + `hint` — follow the hint
    instead of retrying filter variations.

    Filters take a single value OR a list (OR within a list, AND across
    filters). `city`: exact name; `area`: case-insensitive substring
    (combine with `city` so same-named areas elsewhere don't match);
    `property_type` / `asset_category` / `bank` / `branch_name` /
    `auction_type`: exact enum names — expand user phrasing via the shared
    synonym map BEFORE calling.

    `order_by` ∈ "deadline_asc" (default), "deadline_desc", "price_asc",
    "price_desc". For "cheapest/soonest/most-expensive N" use ordering +
    `limit=N`; never invent `min_price`/`max_price`/date thresholds.

    Aggregations — for "price range"/"median"/"average"/"spread": set
    `aggregate_field` to "reserve_price_num" or "emd_num" and `aggregations`
    to any subset of ["min","max","avg","median","p25","p75"]; pass
    `limit=0` to skip the row fetch when only stats are needed. Results are
    added under an `aggregations` key.
    """
    # UI overflow rows ride on ToolReturn metadata (never model-visible) —
    # see api/tool_returns.py for why a plain dict return leaked them into
    # every same-turn round-trip.
    return split_ui_overflow(T.search_auctions(
        min_price=min_price, max_price=max_price,
        city=city, area=area,
        property_type=property_type, asset_category=asset_category,
        bank=bank,
        auction_type=auction_type, branch_name=branch_name,
        starts_after=starts_after, starts_before=starts_before,
        limit=limit, order_by=order_by,
        aggregate_field=aggregate_field, aggregations=aggregations,
        include_past=include_past,
    ))


@agent.tool_plain
def upcoming_auctions(days: int = 14, limit: int = 20) -> list[dict]:
    """Auctions with application deadline within N days."""
    return T.upcoming_auctions(days, limit)


@agent.tool_plain
def borrower_lookup(borrower_name: str) -> list[dict]:
    """Find properties tied to a borrower (substring match)."""
    return T.borrower_lookup(borrower_name)


@agent.tool_plain
def semantic_search(
    query: str,
    city: str | None = None,
    area: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    asset_category: str | None = None,
    starts_after: datetime | None = None,
    starts_before: datetime | None = None,
    limit: int = 20,
    include_past: bool = False,
) -> dict:
    """Semantic search over descriptions, notice markdown, and notice
    images (one gemini-embedding-2 call ranked across three indexes in the
    same vector space). Use for qualitative text — boundaries, neighborhood,
    legal caveats, condition, layout — present in free text or the notice
    but absent from structured fields. For a PASTED listing (WhatsApp/broker
    note with a price, date, or area) use `match_pasted_listing` instead.

    Optional `city`/`area`/`min_price`/`max_price`/`asset_category`/date
    window post-filter the hits. Future-only by default; `include_past=True`
    for retrospective queries. Each row carries `score` and `hit_sources`
    (subset of 'desc'/'markdown'/'image'). A zero-hit result carries a
    `hint` (and `past_matches` when past auctions would have matched) —
    follow it; do NOT rephrase and retry. On embedding-backend failure
    returns `{"error": ..., "results": []}` — fall back to `search_auctions`.
    """
    try:
        return T.semantic_search(
            query, city=city, area=area,
            min_price=min_price, max_price=max_price,
            asset_category=asset_category,
            starts_after=starts_after, starts_before=starts_before,
            limit=limit, include_past=include_past,
        )
    except RuntimeError as e:
        return {"error": str(e), "results": [], "returned": 0, "limit": limit}


@agent.tool_plain
def match_pasted_listing(pasted_text: str) -> dict:
    """Match a pasted property listing (WhatsApp forward, broker note, bank
    circular) to an auction. Use whenever the user pastes a blurb with a
    price / EMD / auction date / building / plot / area / PIN, even if messy.
    Always preferred over `semantic_search` for this.

    Anchors on reserve price ±2% AND auction date ±2 days, widening if
    nothing strict-matches. Returns {match, confidence (0-1), candidates
    (≤5), widening_reason, extracted}. Present by confidence:
      - ≥ 0.85 → "Found it: <match>".
      - 0.6–0.85 → "Very likely this property" + ask to confirm.
      - < 0.6 (widened) → "Couldn't find this exact property; closest
        matches (<widening_reason>):" — list candidates, quote
        `widening_reason` verbatim.
      - match None & candidates empty → say so; ask for auction_id or
        clearer price/date."""
    return T.match_pasted_listing(pasted_text)


@agent.tool_plain
def get_auction_detail(auction_id: str) -> dict | None:
    """Full record for ONE auction_id — every stored property plus
    related city/area/state/bank/borrower/category/property_types and
    `price_history` (re-auction timeline). Call this before concluding
    a field is unavailable for a specific auction. Returns None if the
    auction_id doesn't exist. For rows on SEVERAL known ids, one
    `select_properties(ids)` call replaces N detail calls — only loop
    detail when each id's deep fields/`price_history` are needed."""
    return T.get_auction_detail(auction_id)


@agent.tool_plain
def score_auction(auction_id: str) -> dict | None:
    """Investment score for ONE auction_id: the 10-dimension framework
    (price, location, legal clarity, bank, condition, timeline, DD-ease,
    area trend, competition, yield) computed live from the graph. Returns
    {composite_score 0-100, grade A+..F, dimensions:[{name, score, weight,
    rationale}]}; None if the id doesn't exist. Use for "score/rate/grade
    this auction" and inside the compare/report modes. Read-only."""
    result = _score_auction(auction_id)
    return result.to_dict() if result else None


@agent.tool_plain
def select_properties(auction_ids: list[str]) -> dict:
    """Mirror a subset of already-found properties into the UI matches
    panel — call it whenever you re-present earlier results WITHOUT a new
    search ("top three of those", one locality, a comparison shortlist),
    passing auction_ids in your ranked order. Also the ONE-call way to get
    rows for several known ids (instead of a `get_auction_detail` loop,
    unless each id's deep fields / `price_history` are needed). Returns
    full search-shaped rows; unknown ids come back in `missing_ids`. Skip
    it when a search/detail call this turn already returned exactly that
    set."""
    return T.get_auctions_by_ids(auction_ids)


@agent.tool_plain
def list_distinct(
    field: str,
    limit: int = 100,
    city: str | list[str] | None = None,
    bank: str | list[str] | None = None,
    borrower: str | list[str] | None = None,
    asset_category: str | list[str] | None = None,
    auction_type: str | list[str] | None = None,
    branch: str | list[str] | None = None,
) -> dict:
    """Distinct values of a reference field with per-value auction counts —
    use for distribution / breakdown / "spread" / "mix" questions.

    `field` ∈ {"city","area","state","bank","branch","borrower",
    "asset_category","property_type","auction_type"}.

    Optional scope filters narrow the count: `city`, `bank`, `borrower`,
    `asset_category`, `auction_type`, `branch` (each single str or list,
    any-match). Scope must differ from `field`. Example: property-type
    mix for SBI → field="property_type", bank="State Bank of India"."""
    return T.list_distinct(
        field,
        limit,
        city=city,
        bank=bank,
        borrower=borrower,
        asset_category=asset_category,
        auction_type=auction_type,
        branch=branch,
    )


@agent.tool_plain
def describe_schema(refresh: bool = False) -> dict:
    """Graph schema: labels, relationship types, enum values, numeric/
    date ranges, and `cypher_patterns` (must-know MATCH-shape rules,
    DATETIME handling, ready-to-adapt examples). Cached for 1 hour.
    Call this BEFORE composing a novel `run_cypher` query."""
    return T.describe_schema(refresh)


@agent.tool_plain
def run_cypher(
    cypher: str,
    params: dict | None = None,
    description: str = "",
    max_rows: int = 200,
) -> dict:
    """READ-ONLY Cypher escape hatch for novel queries the specialized
    tools can't express. Writes (CREATE/MERGE/DELETE/SET/REMOVE/DROP/
    LOAD CSV/FOREACH and write procedures) are rejected. Caps: 4000
    chars query, 10s execution, `max_rows` (default 200, hard cap 500).

    Always prefer specialized tools when one fits. Call
    `describe_schema()` first if unsure about labels or properties.

    `description` is a one-sentence intent summary shown in the UI chip."""
    return T.run_cypher(cypher, params, description, max_rows)


@agent.tool_plain
async def internet_search(query: str, max_results: int = 5) -> dict:
    """Public web search (Tavily) for OFF-graph context: SARFAESI/legal
    explainers, RBI/bank news, locality background, term definitions.
    NEVER for property listings, prices, auction_ids, or counts.

    Retry at most once per turn; on `{error}` tell the user web search is
    unavailable. Cite inline as [1], [2] matching the order of `sources`;
    the UI renders the chip list automatically — do NOT print a "Sources:"
    footer.

    Returns {sources: [{title, url, snippet, domain, score}], query} or
    {error: str} on failure."""
    return await W.internet_search(query, max_results=max_results)


# Context-aware (RunContext) tools — they read the caller's supabase_id off
# ChatDeps, so they're per-user. The user identity never reaches the model
# (pydantic-ai strips RunContext from the tool schema); the model just calls
# watch_property(auction_id) / list_alerts().
@agent.tool
async def watch_property(ctx: RunContext[ChatDeps], auction_id: str) -> dict:
    """Save a property to the user's watchlist and turn on its auction-
    deadline alerts. This is THE way to "track"/"monitor"/"watch"/"set up
    alerts" for a property — call it when the user asks for any of those.

    Idempotent: saving an already-saved property is fine. Covers deadline
    timing only — it does NOT watch for price drops, status changes, or send
    email/SMS.

    Returns one of:
      - {status: "watching", auction_id, alerts: [...]} — saved; `alerts` is
        any deadline alert now active for it (empty if the deadline is more
        than 7 days out). Tell the user it's being tracked and surface the
        alert if present.
      - {status: "not_found", auction_id} — no such auction_id; don't claim
        it was saved.
      - {status: "login_required"} — the user isn't signed in; tell them to
        sign in to save and track properties."""
    sub = ctx.deps.supabase_id if ctx.deps else None
    if not sub:
        return {"status": "login_required"}
    saved = await watchlist_repo.add_saved(sub, auction_id)
    if not saved:
        return {"status": "not_found", "auction_id": auction_id}
    rows = await alerts_repo.deadlines_for_ids([auction_id])
    alerts = build_alerts(rows, datetime.now(timezone.utc))
    return {"status": "watching", "auction_id": auction_id, "alerts": alerts}


@agent.tool
async def list_alerts(
    ctx: RunContext[ChatDeps], auction_id: str | None = None
) -> dict:
    """List active auction-deadline alerts on the user's saved properties —
    those whose application deadline is within 7 days (severity "urgent" ≤1d,
    "soon" ≤3d, "upcoming" ≤7d). Pass `auction_id` to check a single saved
    property. Use this for "what's coming up / due", "any deadlines on my
    saved ones", or after `watch_property` to confirm.

    Returns {status, alerts: [...], count}. {status: "login_required"} when
    the user isn't signed in — tell them to sign in. An empty list means
    nothing saved is due within 7 days (not an error)."""
    sub = ctx.deps.supabase_id if ctx.deps else None
    if not sub:
        return {"status": "login_required", "alerts": [], "count": 0}
    rows = await alerts_repo.deadlines_for_saved(sub)
    if auction_id:
        rows = [r for r in rows if r.get("auction_id") == auction_id]
    alerts = build_alerts(rows, datetime.now(timezone.utc))
    return {"status": "ok", "alerts": alerts, "count": len(alerts)}


async def query_user_dossier(
    ctx: RunContext[ChatDeps],
    query: str,
    dossier_id: str | None = None,
    doc_type: str | None = None,
) -> dict:
    """Answer questions about the SIGNED-IN user's OWN uploaded documents — their
    private property dossier (sale deeds, mother deed, EC, patta, FMB, tax
    receipts, approvals, etc.). Use this whenever the user asks about *their*
    documents/dossier ("what does my EC say about prior charges", "is there a
    mortgage in my deed", "summarize the patta I uploaded", "what's missing
    from my dossier"). NEVER use the graph/`run_cypher` for the user's private
    documents — those live only here.

    `query` is the natural-language question (drives keyword retrieval).
    Optional `dossier_id` scopes to one property's dossier; optional `doc_type`
    narrows to a taxonomy type (e.g. "encumbrance_certificate", "sale_deed").

    Returns one of:
      - {status: "ok", matches: [{filename, doc_type, dossier_id, excerpt, …}],
        count} — ground your answer in the `excerpt`s and cite by `filename` /
        `doc_type`; quote, don't invent. If the excerpts don't contain the
        answer, say so plainly.
      - {status: "no_documents", note} — the user has nothing processed in
        scope; tell them to upload documents to their dossier first.
      - {status: "login_required"} — the user isn't signed in; tell them to sign
        in to use their private dossier."""
    sub = ctx.deps.supabase_id if ctx.deps else None
    return await dossier_qa.answer_from_dossier(
        sub, query, dossier_id=dossier_id, doc_type=doc_type,
    )


# Registered only when the dossier feature is enabled (see dossiers_enabled).
# Shipping it dark keeps the tool schema out of the model's context for the
# public release, so the assistant never surfaces a feature users can't reach.
if dossiers_enabled():
    agent.tool(query_user_dossier)
