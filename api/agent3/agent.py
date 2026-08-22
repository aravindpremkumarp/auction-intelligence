"""
api/agent3/agent.py
-------------------
Builds the agent for one turn, on `langchain.agents.create_agent` rather than
`deepagents.create_deep_agent`.

**Why not create_deep_agent.** It unconditionally binds nine tools we cannot
use and cannot remove — `subagents=[]` does not suppress `task`, and a
`HarnessProfile(excluded_tools=...)` registered against a pre-built model does
not drop them from the tool node either (verified against 0.7.7 during the
loop A/B). Measured here: those filesystem/shell schemas cost **~2,611 tokens
in every prompt** for a chat agent that never touches a file. `create_agent`
gives the same LangGraph engine with the tool surface we choose.

We keep the parts of the Deep Agents pattern that earned their place:

| Feature          | How we get it                                        |
|------------------|------------------------------------------------------|
| Transcript memory| `api/checkpointer.py::Neo4jSaver`, reused             |
| Skills           | `api/agent3/skills.py` — our loader, no tool bound    |
| Filesystem/shell | **Not bound.** Nothing here reads or writes a file.   |
| Todo list        | **Not bound.** The plan is the answer's outline.      |

**Cache discipline is the load-bearing design constraint here.** The loop A/B
found the deep agent hitting 24% prompt cache overall and *zero* on the call
that writes the answer, which made its cost number meaningless. So:

- the system prompt is `instructions.md` **verbatim and byte-identical on
  every turn** — no date, no graph size, no per-turn text spliced into it;
- anything that varies per turn (loaded skill text) is appended to the
  *human* message instead, after the cacheable prefix;
- `test_agent3_agent.py` asserts the byte-identity across turns, because a
  prefix that drifts is invisible until someone reads a bill.

Nothing here is wired to a request path. `api/agent3/loop.py` runs a turn;
mounting it behind an endpoint is a later step.
"""
from __future__ import annotations

import functools
import inspect
from pathlib import Path
from typing import Any, Callable

from api.agent3.benchmark_price import benchmark_price
from api.agent3.common import ToolSink
from api.agent3.find_by_identifier import find_by_identifier
from api.agent3.find_properties import find_properties
from api.agent3.get_property import get_property
from api.agent3.reauction_history import reauction_history
from api.agent3.search_notices import search_notices

INSTRUCTIONS_PATH = Path(__file__).resolve().parent / "instructions.md"

#: Model calls one turn may make. A ReAct turn legitimately needs several
#: (think -> tool -> think -> answer); beyond this it is looping, not working.
#: The spike measured 9 on the vanilla harness, which is the pathology.
RUN_MODEL_CALL_LIMIT = 6

#: Tool calls one turn may make. Above this, the agent is grinding.
RUN_TOOL_CALL_LIMIT = 10

#: OpenRouter can be slow; the A/B saw 3.4-9.7 tok/s on the same model
#: minutes apart. Generous, because a timeout mid-answer is worse than a slow
#: answer.
MODEL_TIMEOUT_S = 90.0


def instructions() -> str:
    """The core prompt, read verbatim.

    Deliberately not formatted, templated or f-stringed: this text is the
    cache prefix, and any per-turn substitution silently halves the cache
    hit rate.
    """
    return INSTRUCTIONS_PATH.read_text(encoding="utf-8")


def _drop_param(fn: Callable, name: str) -> Callable:
    """Hide an internal parameter from the schema LangChain infers.

    `find_properties(sink=...)` takes a `ToolSink`, which is per-turn server
    state, not a model argument — and pydantic cannot even build a JSON
    schema for it (`PydanticInvalidForJsonSchema`). Stripping it from
    `__signature__` is what keeps the tool describable AND keeps the sink out
    of the model's reach.
    """
    sig = inspect.signature(fn)
    kept = [p for n, p in sig.parameters.items() if n != name]

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        return fn(*args, **kwargs)

    wrapped.__signature__ = sig.replace(parameters=kept)
    return wrapped


def bind_tools(sink: ToolSink | None = None) -> list[Callable]:
    """The six graph tools, with per-turn state closed over.

    The sink carries the panel's rows so they never enter the transcript: in
    a checkpointed conversation an unsplit 500-row payload is re-sent, and
    re-billed, on every later turn.
    """
    others = [get_property, search_notices, find_by_identifier,
              benchmark_price, reauction_history]
    if sink is None:
        return [_drop_param(find_properties, "sink"), *others]

    bound = functools.partial(find_properties, sink=sink)
    functools.update_wrapper(bound, find_properties)
    return [_drop_param(bound, "sink"), *others]


def chat_model(model_name: str = "flash", reasoning_effort: str | None = None):
    """A LangChain chat model on OpenRouter.

    Imported lazily — `langchain_openai` is ~28 MB of RSS and must not load
    on a deploy that never reaches this package.

    `max_retries=0` on purpose: `ModelRetryMiddleware` owns retries, and two
    retry layers stacked multiply rather than add.
    """
    from langchain_openai import ChatOpenAI

    from api.model_selection import CHAT_MODEL_SLUGS, build_model_settings
    from pipeline.config import OPENROUTER_BASE_URL

    import os

    slug = CHAT_MODEL_SLUGS.get(model_name) or CHAT_MODEL_SLUGS["flash"]
    settings = build_model_settings(reasoning_effort)
    return ChatOpenAI(
        model=slug,
        api_key=(os.getenv("OPENROUTER_CHAT_API_KEY")
                 or os.getenv("OPENROUTER_API_KEY") or ""),
        base_url=OPENROUTER_BASE_URL,
        temperature=0,
        timeout=MODEL_TIMEOUT_S,
        max_retries=0,
        extra_body=settings.get("extra_body") or {},
    )


def middleware(run_limit: int = RUN_MODEL_CALL_LIMIT, *,
               gates: bool = True) -> list[Any]:
    """The deliberately short stack.

    Order matters in one place: `AnswerGate` must sit **after**
    `ModelCallLimitMiddleware` in this list. The limit middleware counts a
    call in its own `after_model`, and a repair the gate triggers is a real
    model call that has to be counted against the ceiling — otherwise a
    repair loop is invisible to the only thing that bounds it.

    Not included, each for a reason:
      - Filesystem/shell/todo  — see the module docstring.
      - SummarizationMiddleware — warranted once transcripts are long, but it
        needs a measured trim threshold rather than a guessed one. Deferred
        until there are real conversations to measure.
      - `PIIMiddleware` — see `gates.IntentGate` for why it does not fit.
    """
    from langchain.agents.middleware import (
        ModelCallLimitMiddleware,
        ModelRetryMiddleware,
        ToolCallLimitMiddleware,
        ToolErrorMiddleware,
    )

    from api.agent3.gates import AnswerGate, IntentGate

    stack: list[Any] = [
        ModelRetryMiddleware(max_retries=2, backoff_factor=2.0,
                             retry_on=should_retry_model_call),
        ModelCallLimitMiddleware(run_limit=run_limit, exit_behavior="end"),
        ToolCallLimitMiddleware(run_limit=RUN_TOOL_CALL_LIMIT,
                                exit_behavior="continue"),
        # Our tools already return {"error": ...} for bad arguments via
        # common.tool; this catches anything that still raises so one bad
        # call cannot kill the turn.
        ToolErrorMiddleware(on_error=tool_error_content),
    ]
    if gates:
        stack.insert(0, IntentGate())
        stack.append(AnswerGate())
    return stack


def should_retry_model_call(exc: Exception) -> bool:
    """Retry transient provider failures only — never our own bugs.

    `ModelRetryMiddleware`'s default is `retry_on=(Exception,)`, which retries
    *everything*. Caught while compiling this graph against a fake model: a
    deterministic `NotImplementedError` was retried three times with
    exponential backoff before failing anyway. On a real deploy that shape of
    mistake — a 4xx from a malformed request, a bad API key, a schema the
    provider rejects — burns three calls and ~7s of backoff to arrive at the
    same error, and does it on every single turn.

    So the rule the design asked for, actually implemented: 5xx and timeouts
    retry, everything else surfaces immediately.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if isinstance(status, int):
        return 500 <= status < 600 or status == 429

    name = type(exc).__name__.lower()
    if "timeout" in name or "ratelimit" in name:
        return True
    # Connection-level failures never reached the provider, so a retry is
    # free of the "same bad request twice" problem.
    return isinstance(exc, (ConnectionError, TimeoutError))


def tool_error_content(exc: Exception, *_args: Any, **_kwargs: Any) -> str:
    """Turn a tool exception into something the model can act on.

    Two tiers, and the split is a disclosure decision rather than a
    formatting one. `ValueError`/`TypeError` are the model getting an
    argument wrong, and the messages are ones we wrote in
    `common.require_enum` / `common.ToolInputError` — they name the valid
    values, so passing them through is what lets the model self-correct.

    Anything else is an internal failure whose message we do not control: a
    Neo4j driver error can carry a URI, a credential or a query fragment.
    LangChain's own guidance is to name the type rather than echo the
    message, so that is all that leaves this function.
    """
    if isinstance(exc, (ValueError, TypeError)):
        return f"Tool error: {exc}"
    return (f"The tool failed with an internal error ({type(exc).__name__}). "
            f"Do not retry it with the same arguments — say plainly that the "
            f"lookup failed.")


def build_agent(*, model_name: str = "flash", reasoning_effort: str | None = None,
                sink: ToolSink | None = None, checkpointer: Any = None,
                run_limit: int = RUN_MODEL_CALL_LIMIT, gates: bool = True):
    """Compile the agent graph.

    `checkpointer` is a `Neo4jSaver` in real use — passed in rather than
    constructed here so tests can run the whole graph in memory.

    `gates=False` builds the same agent without `IntentGate`/`AnswerGate`. It
    exists for the A/B that measures what they cost and catch, not as a
    production switch — a gated and an ungated run have to be comparable on
    the same graph for the numbers in the design doc to mean anything.
    """
    from langchain.agents import create_agent

    return create_agent(
        model=chat_model(model_name, reasoning_effort),
        tools=bind_tools(sink),
        system_prompt=instructions(),
        middleware=middleware(run_limit, gates=gates),
        checkpointer=checkpointer,
    )
