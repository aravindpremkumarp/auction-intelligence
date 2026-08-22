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
    auction.obs agent3.turn.usage in_tok=18422 cached_tok=16128 out_tok=311

Two emitters, one channel. :func:`timed` answers "how long did it take";
:func:`record` answers "what did it cost" — token counts, call counts, the
per-model-call breakdown. Same logger and same `k=v` shape, so one grep (and
one Logfire query) reads both.

**Fields are attached to the LogRecord as well as formatted into the
message.** `api/telemetry.py` ships this logger to Logfire, and a handler
reads structured fields off the record — so `in_tok` is a queryable attribute
there, not something a reader has to regex out of a string.
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


#: LogRecord attribute names we must not shadow. `logging` raises
#: "Attempt to overwrite %r in LogRecord" if an `extra` key collides with a
#: built-in one, and a field named `module` or `name` is an easy accident.
_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


def _extra(fields: dict[str, Any]) -> dict[str, Any]:
    """Fields as LogRecord attributes, with colliding names suffixed."""
    return {(f"{k}_" if k in _RESERVED else k): v
            for k, v in fields.items() if v is not None}


def record(op: str, **fields: Any) -> None:
    """Log one structured fact that has no duration of its own.

    Exists because the thing worth recording about a chat turn is not only
    its latency: the token counts, the per-model-call breakdown and the tool
    tally have no elapsed time to hang off, and inventing a zero-length
    `timed()` block for them would log a lie. Emitted on the same logger as
    :func:`timed` so both land in the same place.
    """
    logger.info("%s %s", op, _fmt_fields(fields),
                extra=_extra({**fields, "op": op}))


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
            extra=_extra({**extra, "op": op, "elapsed_ms": round(elapsed_ms)}),
        )
        raise
    else:
        elapsed_ms = (time.perf_counter() - start) * 1000
        budget = SLOW_QUERY_MS if slow_ms is None else slow_ms
        level = logging.WARNING if elapsed_ms >= budget else logging.INFO
        logger.log(
            level, "%s status=ok elapsed_ms=%.0f %s",
            op, elapsed_ms, _fmt_fields(extra),
            extra=_extra({**extra, "op": op, "elapsed_ms": round(elapsed_ms)}),
        )
