"""
Shared tool surface for the spike — the production implementations from
api/tools/cypher_tools.py, wrapped thin:

- no pydantic-ai / FastAPI imports, so the spike runs standalone;
- ISO-string datetimes coerced to datetime (LLMs emit strings);
- `_ui_results` overflow stripped (browser payload, not model context);
- docstrings kept to a few lines — the schema the model sees IS the
  experiment's slim tool surface.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

# Run standalone (`python experiments/deepagent-chat/...`) without installing
# the repo as a package.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Default to Aura's HTTPS Query API — Bolt (7687) is blocked behind
# HTTP-only egress proxies, and the HTTP path works everywhere.
os.environ.setdefault("NEO4J_HTTP_API", "1")

import api.neo4j_client as _NC  # noqa: E402

# ── spike shim: datetime params over the HTTPS Query API ────────────────────
# `search_auctions` always sets a datetime param (the future-only floor), and
# the production `_http_run` json.dumps's parameters raw — so NEO4J_HTTP_API
# mode crashes on any dated search. Bolt never hits this, which is why it
# hasn't surfaced. Verified live: Aura's Query API v2 accepts typed params as
# {"$type": "OffsetDateTime", "_value": <iso>} under the
# `application/vnd.neo4j.query` content type, preserving datetime-vs-datetime
# comparison semantics (an untyped ISO string would silently match nothing).
# Kept as a monkeypatch so the spike stays additive; the real fix belongs in
# api/neo4j_client.py.
from datetime import datetime as _datetime  # noqa: E402
import base64 as _b64  # noqa: E402
import json as _json  # noqa: E402
import urllib.request as _urlreq  # noqa: E402

_ORIG_HTTP_RUN = _NC._http_run


def _enc(v):
    """Encode one value in the Query API's typed-parameter format. The
    vnd.neo4j.query content type is all-or-nothing: every parameter must be
    typed, so this covers the full JSON-ish value space our tools pass."""
    if v is None:
        return None
    if isinstance(v, bool):  # before int — bool subclasses int
        return {"$type": "Boolean", "_value": v}
    if isinstance(v, _datetime):
        return {"$type": "OffsetDateTime", "_value": v.isoformat()}
    if isinstance(v, str):
        return {"$type": "String", "_value": v}
    if isinstance(v, int):
        return {"$type": "Integer", "_value": str(v)}
    if isinstance(v, float):
        return {"$type": "Float", "_value": str(v)}
    if isinstance(v, (list, tuple)):
        return {"$type": "List", "_value": [_enc(x) for x in v]}
    if isinstance(v, dict):
        return {"$type": "Map", "_value": {k: _enc(x) for k, x in v.items()}}
    raise TypeError(f"unsupported Cypher param type: {type(v).__name__}")


def _http_run_typed(cypher, params, access_mode, timeout):
    params = params or {}
    has_dt = any(
        isinstance(v, _datetime)
        or (isinstance(v, (list, tuple)) and any(isinstance(x, _datetime) for x in v))
        for v in params.values()
    )
    if not has_dt:
        return _ORIG_HTTP_RUN(cypher, params, access_mode, timeout)
    typed = {k: _enc(v) for k, v in params.items()}
    body = _json.dumps({
        "statement": cypher, "parameters": typed, "accessMode": access_mode,
    }).encode("utf-8")
    auth = _b64.b64encode(
        f"{_NC.NEO4J_USERNAME}:{_NC.NEO4J_PASSWORD}".encode()).decode()
    req = _urlreq.Request(
        _NC._http_query_url(), data=body,
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/vnd.neo4j.query",
                 "Accept": "application/json"})
    with _urlreq.urlopen(req, timeout=timeout) as resp:
        payload = _json.loads(resp.read())
    fields = payload["data"]["fields"]
    return [dict(zip(fields, row)) for row in payload["data"]["values"]]


_NC._http_run = _http_run_typed
# ─────────────────────────────────────────────────────────────────────────────

from api.tools import cypher_tools as T  # noqa: E402


def _dt(v: str | datetime | None) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _strip_ui(result: dict) -> dict:
    if isinstance(result, dict):
        result.pop("_ui_results", None)
    return result


def _model_visible_errors(fn):
    """Return validation errors as data instead of raising. Production
    (pydantic-ai) feeds tool errors back to the model; deepagents' tool node
    re-raises them and kills the whole turn (observed: an invalid
    `aggregate_field` crashed variant A's run). The error text carries the
    valid values, so the model can self-correct on the next step."""
    import functools

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, TypeError) as e:
            return {"error": str(e)}

    return wrapped


@_model_visible_errors
def search_auctions(
    min_price: float | None = None,
    max_price: float | None = None,
    city: str | list[str] | None = None,
    area: str | list[str] | None = None,
    property_type: str | list[str] | None = None,
    asset_category: str | list[str] | None = None,
    bank: str | list[str] | None = None,
    borrower: str | list[str] | None = None,
    is_reauction: bool | None = None,
    starts_after: str | None = None,
    starts_before: str | None = None,
    deadline_within_days: int | None = None,
    limit: int = 10,
    order_by: str = "deadline_asc",
    aggregate_field: str | None = None,
    aggregations: list[str] | None = None,
    group_by: str | None = None,
    include_past: bool = False,
) -> dict:
    """Filter/aggregate auction properties. Filters: price, city, area,
    property_type, asset_category, bank, borrower, re-auction flag, date
    window, deadline_within_days. Aggregates via aggregate_field +
    aggregations (min/max/avg/median/p25/p75) or group_by distributions.
    asset_category is one of: Residential | Industrials | Commercial
    ("residential/commercial property" questions filter HERE, not on
    property_type). property_type is the specific kind: Flat, House, Villa,
    Plot, Land, Residential Unit, Land And Building, Commercial Building,
    Commercial Shop, Commercial Property, Agricultural Land, Industrial
    Land, Godown, Shed, Factory land and Building, Machinary, Vehicle, Car,
    Others. Returns rows plus true total_count; on zero rows carries
    `refine` diagnostics — follow them."""
    return _strip_ui(T.search_auctions(
        min_price=min_price, max_price=max_price, city=city, area=area,
        property_type=property_type, asset_category=asset_category,
        bank=bank, borrower=borrower, is_reauction=is_reauction,
        starts_after=_dt(starts_after), starts_before=_dt(starts_before),
        deadline_within_days=deadline_within_days, limit=limit,
        order_by=order_by, aggregate_field=aggregate_field,
        aggregations=aggregations, group_by=group_by,
        include_past=include_past,
    ))


@_model_visible_errors
def semantic_search(
    query: str,
    city: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    limit: int = 10,
) -> dict:
    """Semantic + keyword search over property descriptions and sale-notice
    text/images. Use for qualitative queries: neighbourhood, condition,
    boundaries, legal wording. Optional city/price filters."""
    return _strip_ui(T.semantic_search(
        query=query, city=city, min_price=min_price, max_price=max_price,
        limit=limit,
    ))


@_model_visible_errors
def get_auction_details(auction_ids: list[str]) -> dict:
    """Full records (incl. re-auction price_history) for up to 10 specific
    auction_ids previously returned by a search."""
    return _strip_ui(T.get_auction_details(auction_ids[:10]))


TOOLS = [search_auctions, semantic_search, get_auction_details]

# Optional web search — only when a Tavily key is configured.
if os.getenv("TAVILY_API_KEY"):
    import asyncio
    from api.tools import web_tools as W

    def internet_search(query: str, max_results: int = 5) -> dict:
        """Web search for OFF-graph context only (legal/RBI explainers,
        locality background). Never for prices, counts, or auction_ids."""
        return asyncio.run(W.internet_search(query, max_results=max_results))

    TOOLS.append(internet_search)


def load_instructions() -> str:
    return (Path(__file__).parent / "instructions.md").read_text(encoding="utf-8")


# ── tier 3: the Cypher escape hatch ─────────────────────────────────────────
# Deliberately NOT in TOOLS: the planner never emits run_cypher directly
# (it hasn't seen the schema). Variant B loads these only when the planner
# signals a novel analytical question — the same on-demand shape as
# production's deferred `cypher` capability in api/agent.py.

@_model_visible_errors
def describe_schema(refresh: bool = False) -> dict:
    """Live graph schema: labels, relationships, enums, ranges, and the
    cypher_patterns cheat-sheet. Cached ~1h."""
    return T.describe_schema(refresh=refresh)


@_model_visible_errors
def run_cypher(cypher: str, params: dict | None = None,
               description: str = "") -> dict:
    """READ-ONLY Cypher with production guardrails: write clauses rejected,
    10s timeout, 200-row cap."""
    return T.run_cypher(cypher=cypher, params=params, description=description)


CYPHER_TOOLS = {"describe_schema": describe_schema, "run_cypher": run_cypher}
