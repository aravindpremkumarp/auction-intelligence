"""
api/chat/v2/scope.py
--------------------
Conversation memory as a typed object, not a transcript.

v1 reconstructs "what are we talking about" by walking the whole pydantic-ai
message history every turn (`api/chat/router.py::_extract_active_filters`).
That grows without bound and re-bills the model for it. v2 carries a small
`Scope` instead: the active filters, the ids the last search returned, and its
total count. Turn five costs the same as turn one because the state is a dict,
not a log.

All pure functions:

  sanitize_scope  — trust boundary. The client echoes the scope back, so it is
                    untrusted input on every request.
  merge_scope     — deterministic carry-forward. Code merges, not the model.
  harvest_scope   — read the new scope out of what actually ran.
  harvest_entities— the names this turn put in front of the user, so the next
                    turn's "these areas" has something to point at.

The filter keys come from `api/chat/scope_keys.py`, shared with the v1 router.
"""
from __future__ import annotations

from typing import Any

from api.chat.panel import MAX_PANEL_SYNC_IDS
from api.chat.scope_keys import CARRY_FORWARD_FILTER_KEYS

# What a filter value may be. `search_auctions` takes scalars or lists of them
# (OR within a list, AND across filters); anything else is a client that has
# gone off-contract.
_SCALARS = (str, int, float, bool)

# Bound on a single list-valued filter (e.g. city=[...]). Long enough for any
# real narrowing, short enough that a crafted request can't build a giant
# Cypher IN-list.
MAX_FILTER_LIST = 20

# Bound on one string filter value. City and bank names are short; anything
# longer is not a filter.
MAX_FILTER_STR = 200


def sanitize_scope(raw: Any) -> dict[str, Any]:
    """Return a scope dict safe to merge into `search_auctions` kwargs.

    This is a **trust boundary**, and the reason it is stricter than anything
    v1 needed. v1's `message_history` was inert: pydantic-ai parsed it, and the
    model only ever read it as prose. The v2 scope is merged into tool kwargs
    **by code**, so an unvalidated key here is filter injection — a client
    could pass a `search_auctions` argument the product never meant to expose,
    or a list long enough to be a denial-of-service.

    Rules: keys must be in `CARRY_FORWARD_FILTER_KEYS`; values must be a
    scalar, `None`, or a bounded list of scalars; strings are length-capped.
    Anything else is dropped silently — a malformed scope should degrade to a
    broader search, never to an error the user cannot act on.
    """
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in CARRY_FORWARD_FILTER_KEYS:
            continue
        cleaned = _clean_value(value)
        if cleaned is not _DROP:
            clean[key] = cleaned
    return clean


class _Drop:
    """Sentinel: this value is not usable as a filter. Distinct from `None`,
    which is a meaningful scope value ('this filter was explicitly dropped')."""


_DROP = _Drop()


def _clean_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):  # before int — bool subclasses int
        return value
    if isinstance(value, str):
        text = value.strip()
        return text[:MAX_FILTER_STR] if text else _DROP
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple)):
        items = [_clean_value(v) for v in value[:MAX_FILTER_LIST]]
        items = [v for v in items if v is not _DROP and v is not None]
        return items or _DROP
    return _DROP


def sanitize_ids(raw: Any, cap: int = MAX_PANEL_SYNC_IDS) -> list[str]:
    """Bounded, ordered, de-duplicated list of non-empty id strings.

    Order matters — it is the panel's ranked display order — so this preserves
    first appearance rather than sorting.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, (str, int)):
            continue
        text = str(item).strip()
        if not text or len(text) > 64 or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= cap:
            break
    return out


def merge_scope(scope: dict[str, Any], call_args: dict[str, Any]) -> dict[str, Any]:
    """Layer one planned tool call's args on top of the carried scope.

    The model only has to express **changes**: a value it supplies overrides
    the carried one, and an explicit `None` drops that filter. Everything it
    stays silent about is carried forward by code. That is the whole reason
    narrowing works without a transcript — and why it works deterministically
    rather than depending on the model remembering to restate the city.

    Non-scope args (`limit`, `order_by`, `group_by`, …) pass through
    untouched; they shape this call, not the conversation.
    """
    merged = {k: v for k, v in scope.items() if v is not None}
    for key, value in call_args.items():
        if key not in CARRY_FORWARD_FILTER_KEYS:
            merged[key] = value
            continue
        if value is None:
            merged.pop(key, None)   # explicit drop
        else:
            merged[key] = value
    return merged


def harvest_scope(
    executed: list[dict[str, Any]],
    *,
    previous: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int | None, list[str]]:
    """Read the next turn's scope out of the calls that actually ran.

    Harvesting from *executed* args rather than from the model's plan is
    deliberate: the executed args are post-merge, so the scope reflects what
    the database was really asked, not what the model believed it asked.

    Returns `(filters, last_total_count, last_ids)`. Only search-shaped calls
    contribute; a detail lookup or a web search leaves the scope alone.
    """
    filters = dict(previous or {})
    last_total: int | None = None
    last_ids: list[str] = []

    for call in executed:
        if call.get("tool") not in _SEARCH_TOOLS:
            continue
        args = call.get("args") or {}
        for key in CARRY_FORWARD_FILTER_KEYS:
            if key not in args:
                continue
            value = args[key]
            if value is None:
                filters.pop(key, None)
            else:
                filters[key] = value
        result = call.get("result")
        if isinstance(result, dict):
            if "total_count" in result:
                last_total = result["total_count"]
            rows = result.get("results")
            if isinstance(rows, list):
                ids = [
                    str(r.get("auction_id"))
                    for r in rows
                    if isinstance(r, dict) and r.get("auction_id")
                ]
                if ids:
                    last_ids = sanitize_ids(ids)
    return sanitize_scope(filters), last_total, last_ids


# `semantic_property_search` is the pre-rename name, kept so a stored v1
# conversation migrated into v2 still harvests scope correctly.
_SEARCH_TOOLS = {"search_auctions", "semantic_search", "semantic_property_search"}


# ── referring expressions: "these areas", "that bank" ───────────────────────
#
# The scope above answers "what is filtered". It does not answer "what did the
# last answer NAME", and a follow-up routinely refers to that instead of to an
# auction: a turn that lists nine Chennai areas is followed by "which of these
# areas is growing fast?". The ids carried above are auction ids, so "these
# areas" had no referent at all and the planner asked the user to restate the
# list it had just written — the one thing a conversation should never do.
#
# So the entity labels a turn surfaced are carried too, per dimension. They are
# labels, not filters: they are shown to the planner to resolve a reference and
# are never merged into tool kwargs, which is why they live beside
# CARRY_FORWARD_FILTER_KEYS rather than in it.

#: Dimensions a follow-up refers back to by name. Ordered for prompt stability.
ENTITY_DIMS = ("city", "area", "bank", "asset_category", "property_type",
               "borrower")

MAX_ENTITIES_PER_DIM = 12
MAX_ENTITY_CHARS = 80
#: The previous question, carried verbatim so a pronoun has an antecedent.
#: One turn, capped — this is a referent, not a transcript.
MAX_QUESTION_CHARS = 400


def sanitize_question(raw: Any) -> str:
    """Bounded plain text for the previous question. Client-supplied like the
    rest of the scope, and it lands in a prompt, so it is capped here."""
    if not isinstance(raw, str):
        return ""
    return raw.strip()[:MAX_QUESTION_CHARS]


def sanitize_entities(raw: Any) -> dict[str, list[str]]:
    """Bounded {dimension: [label, ...]}, dropping anything off-contract."""
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, list[str]] = {}
    for dim in ENTITY_DIMS:
        values = raw.get(dim)
        if not isinstance(values, (list, tuple)):
            continue
        labels: list[str] = []
        seen: set[str] = set()
        for item in values:
            if not isinstance(item, (str, int, float)):
                continue
            text = str(item).strip()[:MAX_ENTITY_CHARS]
            if not text or text in seen:
                continue
            seen.add(text)
            labels.append(text)
            if len(labels) >= MAX_ENTITIES_PER_DIM:
                break
        if labels:
            clean[dim] = labels
    return clean


def harvest_entities(executed: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Read the names this turn actually put in front of the user.

    Two sources, because a turn names entities two ways:

    * `group_by` — the buckets ARE the answer, and `search_auctions` returns no
      rows at all in that mode, so the distribution is the only record of them;
    * an ordinary search — the rows carry `city` / `area` / `bank` / …, which
      is what the synthesizer groups its table by.

    Separate from `harvest_scope` on purpose: these are display labels, and
    keeping them out of that function keeps its return value the thing that
    gets merged into tool kwargs.
    """
    found: dict[str, list[str]] = {}

    def add(dim: str, value: Any) -> None:
        if dim not in ENTITY_DIMS or value is None:
            return
        for item in (value if isinstance(value, (list, tuple)) else [value]):
            text = str(item).strip()[:MAX_ENTITY_CHARS]
            bucket = found.setdefault(dim, [])
            if text and text not in bucket and len(bucket) < MAX_ENTITIES_PER_DIM:
                bucket.append(text)

    for call in executed:
        result = call.get("result")
        if not isinstance(result, dict):
            continue
        dim = result.get("group_by")
        if isinstance(dim, str):
            for bucket in result.get("distribution") or []:
                if isinstance(bucket, dict):
                    add(dim, bucket.get("value"))
        for row in result.get("results") or []:
            if not isinstance(row, dict):
                continue
            for entity_dim in ENTITY_DIMS:
                add(entity_dim, row.get(entity_dim))
    return sanitize_entities(found)
