"""
api/neo4j_client.py
-------------------
Singleton Neo4j driver used by all agent tools.
Reuses credentials from pipeline/config.py so the pipeline and API share one source.
"""
from __future__ import annotations

from contextlib import contextmanager
from neo4j import GraphDatabase, Driver

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


def run_query(cypher: str, params: dict | None = None) -> list[dict]:
    with session() as s:
        result = s.run(cypher, params or {})
        return [dict(r) for r in result]
