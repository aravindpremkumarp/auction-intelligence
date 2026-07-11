"""
api/neo4j_client.py
-------------------
Singleton Neo4j drivers (sync + async) used by all agent tools.
Reuses credentials from pipeline/config.py so the pipeline and API share one source.

Sync helpers (`run_query` / `run_read_query`) serve the pipeline, scripts,
and FastAPI handlers declared with plain `def` (those run in the threadpool).
Async helpers (`run_query_async` / `run_read_query_async`) back `async def`
request paths — auth, feedback, conversations, watchlist — so a Neo4j
round-trip never blocks the event loop for concurrent chat requests.

When Bolt (port 7687) is blocked — e.g. running inside an HTTP-only egress
proxy like Claude Code on the web — set NEO4J_HTTP_API=1 to route
run_query / run_read_query through Aura's HTTPS Query API on port 443.
The Bolt-backed `session()` / `read_session()` context managers are
unchanged; only the high-level helpers fall back.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import urllib.request
from contextlib import contextmanager

from neo4j import AsyncDriver, AsyncGraphDatabase, GraphDatabase, Driver, Query, READ_ACCESS

from api.observability import SLOW_QUERY_MS, timed
from pipeline.config import (
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE,
    NEO4J_LIVENESS_CHECK_TIMEOUT_S, NEO4J_MAX_CONNECTION_LIFETIME_S,
    NEO4J_CONNECTION_ACQUISITION_TIMEOUT_S,
)

_driver: Driver | None = None
_async_driver: AsyncDriver | None = None

USE_HTTP_API = os.getenv("NEO4J_HTTP_API", "").strip().lower() in {"1", "true", "yes"}

# Pool config shared by the sync + async drivers. Keeps Aura's idle-dropped
# connections from surfacing as SessionExpired (see pipeline/config.py):
# liveness-probe any connection idle past the threshold before reuse, cap
# total connection age, and bound how long a caller waits for a free slot.
# keep_alive rides on top (TCP keepalives) but doesn't defend against a load
# balancer that closes idle connections outright — the liveness check does.
_POOL_KWARGS = {
    "liveness_check_timeout": NEO4J_LIVENESS_CHECK_TIMEOUT_S,
    "max_connection_lifetime": NEO4J_MAX_CONNECTION_LIFETIME_S,
    "connection_acquisition_timeout": NEO4J_CONNECTION_ACQUISITION_TIMEOUT_S,
    "keep_alive": True,
}


def _http_query_url() -> str:
    no_scheme = NEO4J_URI.split("://", 1)[-1]
    host = no_scheme.split("/", 1)[0].split("?", 1)[0]
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    db = NEO4J_DATABASE or "neo4j"
    return f"https://{host}/db/{db}/query/v2"


def _http_run(cypher: str, params: dict | None, access_mode: str, timeout: float) -> list[dict]:
    body = json.dumps({
        "statement": cypher,
        "parameters": params or {},
        "accessMode": access_mode,
    }).encode("utf-8")
    auth = base64.b64encode(f"{NEO4J_USERNAME}:{NEO4J_PASSWORD}".encode()).decode()
    req = urllib.request.Request(
        _http_query_url(),
        data=body,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("errors"):
        err = payload["errors"][0]
        raise RuntimeError(f"Neo4j HTTP API error: {err.get('code')} {err.get('message')}")
    data = payload.get("data") or {}
    fields = data.get("fields") or []
    values = data.get("values") or []
    return [dict(zip(fields, row)) for row in values]


def get_driver() -> Driver:
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD), **_POOL_KWARGS
        )
    return _driver


def get_async_driver() -> AsyncDriver:
    global _async_driver
    if _async_driver is None:
        _async_driver = AsyncGraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD), **_POOL_KWARGS
        )
    return _async_driver


@contextmanager
def session():
    drv = get_driver()
    with drv.session(database=NEO4J_DATABASE) as s:
        yield s


@contextmanager
def read_session():
    """READ-only session — used by run_read_query so mutations can't slip
    through even if the Cypher text bypasses the regex guard."""
    drv = get_driver()
    with drv.session(database=NEO4J_DATABASE, default_access_mode=READ_ACCESS) as s:
        yield s


def run_query(cypher: str, params: dict | None = None) -> list[dict]:
    with timed("neo4j.run_query", slow_ms=SLOW_QUERY_MS, access="write") as t:
        if USE_HTTP_API:
            rows = _http_run(cypher, params, access_mode="WRITE", timeout=120.0)
        else:
            with session() as s:
                result = s.run(cypher, params or {})
                rows = [dict(r) for r in result]
        t["rows"] = len(rows)
        return rows


def run_read_query(
    cypher: str,
    params: dict | None = None,
    timeout: float = 10.0,
    max_rows: int = 200,
) -> list[dict]:
    """Execute a read-only Cypher query with a hard timeout + row cap.

    - Uses a session with READ access mode so mutating clauses that slip
      past caller-side validation still fail at the server.
    - `timeout` bounds server-side transaction time (seconds). It must ride
      on a `neo4j.Query` object — a bare `timeout=` kwarg to `Session.run`
      is silently absorbed as a Cypher parameter and enforces nothing.
    - `max_rows` trims the returned list after fetching. Pair with a LIMIT
      clause in the caller to also bound database work.
    """
    with timed("neo4j.run_read_query", slow_ms=SLOW_QUERY_MS, access="read") as t:
        if USE_HTTP_API:
            out = _http_run(cypher, params, access_mode="READ",
                            timeout=max(timeout, 30.0))[:max_rows]
        else:
            with read_session() as s:
                result = s.run(Query(cypher, timeout=timeout), params or {})
                out = []
                for i, r in enumerate(result):
                    if i >= max_rows:
                        break
                    out.append(dict(r))
        t["rows"] = len(out)
        return out


async def run_query_async(cypher: str, params: dict | None = None) -> list[dict]:
    """Async twin of run_query for `async def` request paths. Same semantics;
    the HTTP-API fallback offloads its blocking urllib call to a thread."""
    with timed("neo4j.run_query", slow_ms=SLOW_QUERY_MS, access="write") as t:
        if USE_HTTP_API:
            rows = await asyncio.to_thread(
                _http_run, cypher, params, "WRITE", 120.0
            )
        else:
            drv = get_async_driver()
            async with drv.session(database=NEO4J_DATABASE) as s:
                result = await s.run(cypher, params or {})
                rows = [dict(r) async for r in result]
        t["rows"] = len(rows)
        return rows


async def run_read_query_async(
    cypher: str,
    params: dict | None = None,
    timeout: float = 10.0,
    max_rows: int = 200,
) -> list[dict]:
    """Async twin of run_read_query: READ access mode, server-side timeout,
    row cap."""
    with timed("neo4j.run_read_query", slow_ms=SLOW_QUERY_MS, access="read") as t:
        if USE_HTTP_API:
            rows = await asyncio.to_thread(
                _http_run, cypher, params, "READ", max(timeout, 30.0)
            )
            out = rows[:max_rows]
        else:
            drv = get_async_driver()
            async with drv.session(
                database=NEO4J_DATABASE, default_access_mode=READ_ACCESS
            ) as s:
                result = await s.run(Query(cypher, timeout=timeout), params or {})
                out = []
                async for r in result:
                    if len(out) >= max_rows:
                        break
                    out.append(dict(r))
        t["rows"] = len(out)
        return out
