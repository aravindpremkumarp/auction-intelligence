"""
api/neo4j_client.py
-------------------
Singleton Neo4j driver used by all agent tools.
Reuses credentials from pipeline/config.py so the pipeline and API share one source.

When Bolt (port 7687) is blocked — e.g. running inside an HTTP-only egress
proxy like Claude Code on the web — set NEO4J_HTTP_API=1 to route
run_query / run_read_query through Aura's HTTPS Query API on port 443.
The Bolt-backed `session()` / `read_session()` context managers are
unchanged; only the high-level helpers fall back.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
from contextlib import contextmanager

from neo4j import GraphDatabase, Driver, READ_ACCESS

from pipeline.config import (
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE,
)

_driver: Driver | None = None

USE_HTTP_API = os.getenv("NEO4J_HTTP_API", "").strip().lower() in {"1", "true", "yes"}


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
            NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD)
        )
    return _driver


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
    if USE_HTTP_API:
        return _http_run(cypher, params, access_mode="WRITE", timeout=120.0)
    with session() as s:
        result = s.run(cypher, params or {})
        return [dict(r) for r in result]


def run_read_query(
    cypher: str,
    params: dict | None = None,
    timeout: float = 10.0,
    max_rows: int = 200,
) -> list[dict]:
    """Execute a read-only Cypher query with a hard timeout + row cap.

    - Uses a session with READ access mode so mutating clauses that slip
      past caller-side validation still fail at the server.
    - `timeout` bounds server-side transaction time (seconds). Honored by
      the neo4j Python driver via the per-query `timeout` keyword.
    - `max_rows` trims the returned list after fetching. Pair with a LIMIT
      clause in the caller to also bound database work.
    """
    if USE_HTTP_API:
        rows = _http_run(cypher, params, access_mode="READ",
                         timeout=max(timeout, 30.0))
        return rows[:max_rows]
    with read_session() as s:
        result = s.run(cypher, params or {}, timeout=timeout)
        out: list[dict] = []
        for i, r in enumerate(result):
            if i >= max_rows:
                break
            out.append(dict(r))
        return out
