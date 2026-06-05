"""
api/observability.py
--------------------
Lightweight, dependency-free observability helpers.

The product's latency-sensitive paths — the LLM chat agent, Neo4j queries,
and tool calls — are timed here and emitted as structured log lines so an
operator can grep Render logs (or ship them to a log drain) for slow
queries, agent latency, and tool-call errors *without* standing up a metrics
backend first. Thresholds are env-tunable; everything degrades to plain
`logging` when unset, so importing this module never fails and never needs
extra dependencies.

Log line shape (space-separated key=value, easy to grep/parse):

    auction.obs neo4j.run_read_query status=ok elapsed_ms=42 rows=18
    auction.obs chat.agent_run status=error elapsed_ms=9120 mode=ask err=...
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger("auction.obs")

# Emit a WARNING (instead of INFO) when an operation exceeds these millisecond
# budgets. Tune per-deploy via env without a code change.
SLOW_QUERY_MS = float(os.getenv("OBS_SLOW_QUERY_MS", "1500"))
SLOW_AGENT_MS = float(os.getenv("OBS_SLOW_AGENT_MS", "12000"))


def _fmt_fields(fields: dict[str, Any]) -> str:
    """Render context fields as `k=v` pairs, dropping Nones for terse lines."""
    return " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)


@contextmanager
def timed(op: str, *, slow_ms: float | None = None, **fields: Any) -> Iterator[dict]:
    """Time a block and log its duration in milliseconds.

    Logs at WARNING when the elapsed time exceeds ``slow_ms`` (default
    :data:`SLOW_QUERY_MS`), else at INFO. The yielded dict can be mutated by
    the caller to attach result fields (row counts, tool counts) that are
    included on exit. On exception it logs at ERROR with the elapsed time and
    a repr of the error, then re-raises so callers keep their own handling.
    """
    extra: dict[str, Any] = dict(fields)
    start = time.perf_counter()
    try:
        yield extra
    except Exception as exc:  # noqa: BLE001 - log + re-raise, don't swallow
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.error(
            "%s status=error elapsed_ms=%.0f %s err=%r",
            op, elapsed_ms, _fmt_fields(extra), exc,
        )
        raise
    else:
        elapsed_ms = (time.perf_counter() - start) * 1000
        budget = SLOW_QUERY_MS if slow_ms is None else slow_ms
        level = logging.WARNING if elapsed_ms >= budget else logging.INFO
        logger.log(
            level, "%s status=ok elapsed_ms=%.0f %s",
            op, elapsed_ms, _fmt_fields(extra),
        )
