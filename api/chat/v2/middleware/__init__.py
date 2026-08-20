"""
api/chat/v2/middleware
----------------------
The four domain checks from `docs/chat-agent-middleware-2026-08.md`:
`IntentGate`, `InjectionEnvelope`, `AnswerGate`, and scope carry/reset.

**Why these are plain functions, not `AgentMiddleware` subclasses.** A
LangChain middleware hook is worth its indirection when it has to run *inside*
an agent graph — around a model call the caller doesn't control, or around a
tool node. None of these do:

* `IntentGate` and `InjectionEnvelope` act on the user's message before any
  model call. On a one-shot agent, a `before_agent` hook and a function
  called before `ainvoke` run at the same moment and see the same data.
* `AnswerGate` has to compare the draft answer against **this turn's tool
  results**, which live in the loop, not in the agent's state. As an
  `after_model` hook it would be checking against data it cannot see.
* Scope carry/reset is already implemented, deterministically, in
  `api/chat/v2/scope.py` — the planner emits `carry | reset` as a typed field
  and code does the merge. Wrapping that in a middleware class would be a
  second copy of logic that has one correct implementation.

The off-the-shelf middleware in `api/chat/v2/agents.py` ARE `AgentMiddleware`
subclasses, because retry and fallback genuinely need to wrap the model call.

Everything here is pure and synchronous. That matters for the cost model: on
a normal turn these add no model call and no measurable latency.
"""
from api.chat.v2.middleware.answer_gate import GateVerdict, check_answer
from api.chat.v2.middleware.injection_envelope import wrap_pasted_content
from api.chat.v2.middleware.intent_gate import IntentVerdict, classify_intent

__all__ = [
    "GateVerdict",
    "IntentVerdict",
    "check_answer",
    "classify_intent",
    "wrap_pasted_content",
]
