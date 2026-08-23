"""
api/agent3/common.py
--------------------
Conventions every agent3 tool obeys, in one place so they cannot drift.

Four rules, each of them the fix for a specific observed failure:

1. **Errors return as data.** The deepagents tool node re-raises a tool
   exception and kills the whole turn — the spike lost a run to one invalid
   `aggregate_field`. A ValueError here comes back as
   `{"error": ..., "valid_values": [...]}` so the model can correct itself.
   A real bug still raises.

2. **Lot facts carry their scope.** A notice fans out to 4.4 lots on average
   and nothing disambiguates which lot is this listing, so a lot-derived value
   is per-property truth ONLY on a single-lot notice. `scope_of()` decides;
   every tool tags every lot-derived value with the answer.

3. **sqft is band-limited.** `Measurement.sqft_norm` has real outliers — the
   max in the graph is 15,571,959,480 against a median of 1,471. One row like
   that poisons every average and every ₹/sqft. Values outside the band are
   excluded and counted, never silently averaged in.

4. **Nothing heavy reaches the model.** Panel rows go to a sink; the model
   sees a slice plus an exact `total_count`. In a checkpointed transcript an
   unsplit payload is re-billed on every later turn for the rest of the
   conversation.
"""
from __future__ import annotations

import functools
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from api.observability import timed

logger = logging.getLogger("api.agent3")

#: Plausible extent band, in square feet. Below the floor is a parse artefact
#: (0.0 appears); above the ceiling is a decimal-point or unit error. 500,000
#: sqft is ~11.5 acres — comfortably above any real single lot in this data,
#: whose p90 is 10,977.
SQFT_FLOOR = 1.0
SQFT_CEIL = 500_000.0

#: Hard cap on rows handed back to the model, whatever `limit` says. The panel
#: still shows every match; this bounds the transcript.
MAX_MODEL_ROWS = 50

#: Hard cap on ids per get_property call. Five full dossiers is already a big
#: tool message.
MAX_DETAIL_IDS = 5


class ToolInputError(ValueError):
    """A bad argument from the model, with the valid values attached.

    Distinct from a plain ValueError so `tool` can return the enumeration
    alongside the message — the model fixes itself far more reliably when it
    is shown the options than when it is only told it was wrong.
    """

    def __init__(self, message: str, valid_values: Any = None, field: str | None = None):
        super().__init__(message)
        self.valid_values = valid_values
        self.field = field


@dataclass
class TurnContext:
    """What the running turn is, visible from inside a tool call.

    A tool is a plain function several frames below the loop; it knows its own
    arguments and nothing else. That is why `agent3.tool` lines carried no
    thread id and could only be tied back to a conversation through the trace
    — and a trace is exactly what is missing when the export is a log drain
    rather than a tracing backend.

    `steps` is the same problem seen from the other end: when a turn raises,
    `ainvoke` returns nothing, so the messages carrying the tool round-trips
    never exist and the transcript of the failing turn is empty. Tools append
    here as they run, so the loop can report what happened *before* the error
    even though the graph gave it nothing.
    """
    thread_id: str
    steps: list[dict] = field(default_factory=list)


#: The turn in progress, or None outside one (an eval script, a direct tool
#: call in a test). LangChain copies the context into the executor it runs
#: sync tools in, so this survives the hop off the event loop; a custom
#: executor would not copy it, which is why every reader tolerates None.
_CURRENT_TURN: ContextVar[TurnContext | None] = ContextVar(
    "agent3_turn", default=None)


@contextmanager
def turn_context(thread_id: str) -> Iterator[TurnContext]:
    """Mark the block as one turn of `thread_id`.

    Reset on the way out, always: a leaked context would label the next
    turn's tool calls with the previous thread, which is worse than no label
    at all.
    """
    ctx = TurnContext(thread_id=thread_id)
    token = _CURRENT_TURN.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT_TURN.reset(token)


def current_turn() -> TurnContext | None:
    """The turn in progress, or None when a tool is called outside one."""
    return _CURRENT_TURN.get()


def _result_fields(out: Any) -> dict[str, Any]:
    """The size of what a tool returned, for the observability line.

    Row counts, never rows. A tool result can carry a 500-row payload and
    the whole point of `ToolSink` is that such a payload does not get copied
    around; a log line that dumped it would undo that at a different layer.
    """
    if not isinstance(out, dict):
        return {}
    fields: dict[str, Any] = {}
    for key in ("rows", "results", "lots"):
        value = out.get(key)
        if isinstance(value, list):
            fields["rows"] = len(value)
            break
    for key in ("total_count", "result_count"):
        value = out.get(key)
        if isinstance(value, int):
            fields["total_count"] = value
            break
    return fields


def tool(fn: Callable) -> Callable:
    """Wrap a tool so bad input comes back as data instead of killing the turn.

    Also the one place every graph tool passes through, so it is where each
    call is timed and recorded. That matters more here than it looks: rule 1
    means a rejected argument returns `{"error": ...}` and the turn carries
    on succeeding, so without this line a tool the model got wrong on every
    call for a week would leave no trace anywhere — the answer still arrives,
    only slower and worse.

    `result` is the outcome the model saw: `ok`, `input_error` (a bad
    argument, with the valid values handed back) or `error` (a plain
    ValueError/TypeError). A tool that raises something else is a real bug
    and still propagates, logged by `timed` at ERROR on the way out.
    """

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        turn = current_turn()
        started = time.perf_counter()
        with timed("agent3.tool", tool=fn.__name__,
                   thread=turn.thread_id if turn else None) as obs:
            try:
                out = fn(*args, **kwargs)
            except ToolInputError as exc:
                obs["result"] = "input_error"
                obs["field"] = exc.field
                out = {"error": str(exc)}
                if exc.field:
                    out["field"] = exc.field
                if exc.valid_values is not None:
                    out["valid_values"] = list(exc.valid_values)
                _note_step(turn, fn, args, kwargs, obs, started)
                return out
            except (ValueError, TypeError) as exc:
                obs["result"] = "error"
                obs["err"] = type(exc).__name__
                _note_step(turn, fn, args, kwargs, obs, started)
                return {"error": str(exc)}
            except BaseException:
                # A real bug, on its way out. The step is worth keeping for
                # exactly the same reason the turn's transcript is: this is
                # the call that broke the turn.
                obs["result"] = "raised"
                _note_step(turn, fn, args, kwargs, obs, started)
                raise
            obs["result"] = "error" if isinstance(out, dict) and out.get("error") else "ok"
            obs.update(_result_fields(out))
            _note_step(turn, fn, args, kwargs, obs, started)
            return out

    return wrapped


def _note_step(turn: TurnContext | None, fn: Callable, args: tuple,
               kwargs: dict, obs: dict, started: float) -> None:
    """Append this call to the turn's live step list.

    Summaries, never payloads — the row counts `timed` already computed, not
    the rows. Rule 4 exists so a tool result is not copied around, and a
    buffer that held full results for the length of a turn would break it in
    a new place.
    """
    if turn is None:
        return
    step = {"tool": fn.__name__,
            "args": {**({"_args": list(args)} if args else {}), **kwargs},
            "ms": round((time.perf_counter() - started) * 1000)}
    step.update({k: v for k, v in obs.items()
                 if k in ("result", "rows", "total_count", "field", "err")})
    turn.steps.append(step)


def json_safe(v: Any) -> Any:
    """Coerce neo4j temporal types to ISO strings, at any nesting depth.

    `properties(node)` hands back raw neo4j Date/Time/DateTime objects, which
    no JSON encoder downstream can serialize.
    """
    if hasattr(v, "iso_format"):
        return v.iso_format()
    if isinstance(v, dict):
        return {k: json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [json_safe(x) for x in v]
    return v


def aware(dt: datetime | str | None) -> datetime | None:
    """Promote a naive datetime to UTC, and parse ISO strings.

    Stored listing dates are ZONED DATETIME. Cypher comparison between a ZONED
    and a LOCAL datetime silently yields zero matches — not an error, just an
    empty result — so a naive datetime arriving from the model must be
    promoted before it reaches a query.
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        raw = dt.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ToolInputError(
                f"Could not read {raw!r} as a date. Use ISO format, "
                f"e.g. '2026-09-01' or '2026-09-01T11:00:00Z'.") from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def scope_of(lot_count: int | None) -> str:
    """`lot` when the notice describes exactly one lot, else `notice`.

    This is the whole scope-honesty mechanism. A `lot`-scoped value is a fact
    about this property. A `notice`-scoped value is a fact about the notice
    the property was listed in, which covers several lots — stating it as a
    property fact is wrong, and the answer gate treats it as a failure.
    """
    if lot_count == 1:
        return "lot"
    return "notice"


def scope_note(field: str, lot_count: int | None) -> str | None:
    """The sentence the agent must carry when a value is notice-scoped."""
    if scope_of(lot_count) == "lot":
        return None
    if not lot_count:
        return (f"No sale-notice lot could be read for this listing, so "
                f"{field} is unknown.")
    return (f"The sale notice covers {lot_count} lots and does not say which "
            f"one this listing is, so {field} describes the notice, not this "
            f"property specifically.")


def band_note(excluded: int) -> str | None:
    """Report extents dropped for being outside the plausible band."""
    if not excluded:
        return None
    return (f"{excluded} recorded extent(s) fell outside "
            f"{int(SQFT_FLOOR)}–{int(SQFT_CEIL):,} sqft and were excluded as "
            f"parse errors.")


def clamp_limit(limit: int | None, default: int = 20) -> int:
    if limit is None:
        return default
    try:
        n = int(limit)
    except (TypeError, ValueError) as exc:
        raise ToolInputError(f"limit must be a whole number, got {limit!r}") from exc
    return max(1, min(n, MAX_MODEL_ROWS))


def require_enum(value: str | None, allowed: tuple[str, ...], field: str) -> str | None:
    """Validate one enum argument, case-insensitively, returning the canonical
    spelling. Raises with the full list attached so the model can self-correct."""
    if value is None:
        return None
    raw = str(value).strip()
    for a in allowed:
        if raw.lower() == a.lower():
            return a
    raise ToolInputError(
        f"{field}={value!r} is not a value this graph uses.",
        valid_values=allowed, field=field)


class ToolSink:
    """Per-turn collector for rows the panel needs and the model must not see.

    A search can match hundreds of listings. The panel renders all of them;
    the model reads a slice. Without this split the full payload becomes a
    ToolMessage that is checkpointed and re-sent on every later turn — the
    exact re-billing the deep-loop A/B measured.
    """

    def __init__(self) -> None:
        self.panel_rows: list[dict] = []
        self.auction_ids: list[str] = []
        #: Web sources cited this turn. Unlike panel_rows these are NOT held
        #: back from the model — it needs them to attribute what it says. The
        #: sink carries them so the response can render citation chips
        #: without re-deriving them from the answer text.
        self.web_sources: list[dict] = []

    def absorb(self, rows: list[dict]) -> None:
        self.panel_rows = rows
        seen: set[str] = set()
        ids: list[str] = []
        for r in rows:
            aid = r.get("auction_id")
            if aid and aid not in seen:
                seen.add(aid)
                ids.append(aid)
        self.auction_ids = ids

    def absorb_web(self, sources: list[dict]) -> None:
        """Accumulate, don't replace — a turn may search more than once.

        Deduplicated by url: two searches on related queries routinely return
        the same page, and a citation list that shows it twice looks careless.
        """
        seen = {s.get("url") for s in self.web_sources}
        for s in sources or []:
            url = s.get("url")
            if url and url not in seen:
                seen.add(url)
                self.web_sources.append(s)
