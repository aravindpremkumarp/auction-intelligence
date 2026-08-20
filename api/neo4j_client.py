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
import logging
import os
import time
import urllib.request
from contextlib import contextmanager
# Aliased: a bare `time` import would shadow the stdlib `time` module used by
# the retry backoff below.
from datetime import date as _date, datetime as _datetime, time as _time

from neo4j import AsyncDriver, AsyncGraphDatabase, GraphDatabase, Driver, Query, READ_ACCESS
from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError

from api.observability import SLOW_QUERY_MS, timed
from pipeline.config import (
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE,
    NEO4J_LIVENESS_CHECK_TIMEOUT_S, NEO4J_MAX_CONNECTION_LIFETIME_S,
    NEO4J_CONNECTION_ACQUISITION_TIMEOUT_S,
    NEO4J_MAX_QUERY_RETRIES, NEO4J_RETRY_BASE_DELAY_S,
)

logger = logging.getLogger("api.neo4j_client")

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

# Transient Neo4j faults worth retrying on a fresh connection. In production
# these fail at connection-acquisition time — SessionExpired("defunct
# connection") when the pool hands out a connection Aura already dropped, and
# ServiceUnavailable("Unable to retrieve routing information") when the driver
# can't refresh its routing table against dead cached connections — so nothing
# has executed yet and re-running the unit of work is safe. TransientError
# covers the driver's general retryable class (deadlock, leader switch). Each
# retry opens a new session, i.e. re-acquires from the pool; combined with the
# liveness probe it lands on a validated connection instead of the dead one.
# (Writes are re-run, so a mid-commit drop could in theory apply twice — the
# same at-least-once caveat the driver's own execute_write carries. The app's
# writes are MERGE/SET upserts, and the observed faults are pre-execution, so
# in practice this is safe.)
_RETRYABLE_NEO4J = (ServiceUnavailable, SessionExpired, TransientError)


def _retry_delay(attempt: int) -> float:
    """Exponential backoff for retry N (1-based): base, 2·base, 4·base…"""
    return NEO4J_RETRY_BASE_DELAY_S * (2 ** (attempt - 1))


def _run_with_retry(work, *, op: str):
    """Run `work()` (a no-arg callable that opens its own session), retrying on
    transient Neo4j faults with a fresh session each time. Non-transient errors
    propagate immediately. Retries are logged at WARNING so an auto-recovered
    blip is visible without tripping the obs status=error line."""
    attempt = 0
    while True:
        try:
            return work()
        except _RETRYABLE_NEO4J as exc:
            attempt += 1
            if attempt > NEO4J_MAX_QUERY_RETRIES:
                raise
            delay = _retry_delay(attempt)
            logger.warning(
                "%s transient error (attempt %d/%d), retrying in %.2fs: %r",
                op, attempt, NEO4J_MAX_QUERY_RETRIES, delay, exc,
            )
            time.sleep(delay)


async def _run_with_retry_async(work, *, op: str):
    """Async twin of `_run_with_retry`; `work` is an async no-arg callable."""
    attempt = 0
    while True:
        try:
            return await work()
        except _RETRYABLE_NEO4J as exc:
            attempt += 1
            if attempt > NEO4J_MAX_QUERY_RETRIES:
                raise
            delay = _retry_delay(attempt)
            logger.warning(
                "%s transient error (attempt %d/%d), retrying in %.2fs: %r",
                op, attempt, NEO4J_MAX_QUERY_RETRIES, delay, exc,
            )
            await asyncio.sleep(delay)


def _http_query_url() -> str:
    no_scheme = NEO4J_URI.split("://", 1)[-1]
    host = no_scheme.split("/", 1)[0].split("?", 1)[0]
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    db = NEO4J_DATABASE or "neo4j"
    return f"https://{host}/db/{db}/query/v2"


# Aura's Query API v2 accepts two parameter encodings. Plain JSON
# (`application/json`) is what we send by default, but it cannot express a
# Cypher temporal: `json.dumps` raises on a datetime, and hand-serializing it
# to an ISO string would compare String-to-DateTime in Cypher and silently
# match nothing. The typed encoding (`application/vnd.neo4j.query`) carries
# the type with the value and preserves comparison semantics.
#
# Two traps, both verified against live Aura:
#   * the typed encoding is ALL-OR-NOTHING per request — mixing a plain value
#     in with typed ones returns HTTP 400, so once one param needs typing
#     every param must be typed;
#   * `Boolean` takes a real JSON bool, not a string, so the `bool` branch
#     must come before the `int` branch (bool subclasses int in Python).
#
# Bolt never hits any of this, which is why it only shows up under
# NEO4J_HTTP_API=1.
_TEMPORAL_TYPES = (_datetime, _date, _time)


def _encode_param(v):
    """Encode one value in the Query API's typed-parameter format."""
    if v is None:
        return None
    if isinstance(v, bool):  # before int — bool subclasses int
        return {"$type": "Boolean", "_value": v}
    if isinstance(v, _datetime):
        return {"$type": "OffsetDateTime" if v.tzinfo else "LocalDateTime",
                "_value": v.isoformat()}
    if isinstance(v, _date):
        return {"$type": "Date", "_value": v.isoformat()}
    if isinstance(v, _time):
        return {"$type": "OffsetTime" if v.tzinfo else "LocalTime",
                "_value": v.isoformat()}
    if isinstance(v, str):
        return {"$type": "String", "_value": v}
    if isinstance(v, int):
        return {"$type": "Integer", "_value": str(v)}
    if isinstance(v, float):
        return {"$type": "Float", "_value": str(v)}
    if isinstance(v, (list, tuple)):
        return {"$type": "List", "_value": [_encode_param(x) for x in v]}
    if isinstance(v, dict):
        return {"$type": "Map",
                "_value": {k: _encode_param(x) for k, x in v.items()}}
    raise TypeError(f"unsupported Cypher param type: {type(v).__name__}")


def _needs_typed_params(value) -> bool:
    """True when any value in the (possibly nested) params needs the typed
    encoding — i.e. a temporal `json.dumps` would refuse to serialize."""
    if isinstance(value, _TEMPORAL_TYPES):
        return True
    if isinstance(value, dict):
        return any(_needs_typed_params(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_needs_typed_params(v) for v in value)
    return False


def _http_run(cypher: str, params: dict | None, access_mode: str, timeout: float) -> list[dict]:
    params = params or {}
    typed = _needs_typed_params(params)
    body = json.dumps({
        "statement": cypher,
        "parameters": (
            {k: _encode_param(v) for k, v in params.items()} if typed else params
        ),
        "accessMode": access_mode,
    }).encode("utf-8")
    auth = base64.b64encode(f"{NEO4J_USERNAME}:{NEO4J_PASSWORD}".encode()).decode()
    req = urllib.request.Request(
        _http_query_url(),
        data=body,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": (
                "application/vnd.neo4j.query" if typed else "application/json"
            ),
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
            def _work() -> list[dict]:
                with session() as s:
                    result = s.run(cypher, params or {})
                    return [dict(r) for r in result]
            rows = _run_with_retry(_work, op="neo4j.run_query")
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
            def _work() -> list[dict]:
                with read_session() as s:
                    result = s.run(Query(cypher, timeout=timeout), params or {})
                    rows: list[dict] = []
                    for i, r in enumerate(result):
                        if i >= max_rows:
                            break
                        rows.append(dict(r))
                    return rows
            out = _run_with_retry(_work, op="neo4j.run_read_query")
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
            async def _work() -> list[dict]:
                drv = get_async_driver()
                async with drv.session(database=NEO4J_DATABASE) as s:
                    result = await s.run(cypher, params or {})
                    return [dict(r) async for r in result]
            rows = await _run_with_retry_async(_work, op="neo4j.run_query")
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
            async def _work() -> list[dict]:
                drv = get_async_driver()
                async with drv.session(
                    database=NEO4J_DATABASE, default_access_mode=READ_ACCESS
                ) as s:
                    result = await s.run(Query(cypher, timeout=timeout), params or {})
                    rows: list[dict] = []
                    async for r in result:
                        if len(rows) >= max_rows:
                            break
                        rows.append(dict(r))
                    return rows
            out = await _run_with_retry_async(_work, op="neo4j.run_read_query")
        t["rows"] = len(out)
        return out
