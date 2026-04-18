"""
api/neo4j_client.py
-------------------
Singleton Neo4j driver used by all agent tools.
Reuses credentials from pipeline/config.py so the pipeline and API share one source.
"""
from __future__ import annotations

from contextlib import contextmanager
from neo4j import GraphDatabase, Driver, READ_ACCESS

from pipeline.config import (
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE,
)

_driver: Driver | None = None


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
    with read_session() as s:
        result = s.run(cypher, params or {}, timeout=timeout)
        out: list[dict] = []
        for i, r in enumerate(result):
            if i >= max_rows:
                break
            out.append(dict(r))
        return out
