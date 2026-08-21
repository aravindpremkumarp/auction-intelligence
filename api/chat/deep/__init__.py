"""
api/chat/deep
-------------
The chat agent on the **Deep Agents harness** — `create_deep_agent`'s ReAct
loop, with real message history checkpointed server-side in Neo4j.

This is the second half of an A/B, not a replacement. `/chat/v2` (the tiered
loop) and `/chat/deep` (this) run the same tools, the same policy, the same
middleware and the same quota; only the loop shape and the memory model
differ. `evals/run_loop_ab.py` scores both on the 68-case golden catalogue so
the choice is made on data rather than on the 4-question smoke set the
original spike used.

Why it exists, in one line: the tiered loop's scope object is a *summary* of
the conversation, and a summary can only answer questions about the things it
chose to summarise. A transcript can answer any question about anything that
was said. `docs/chat-loop-ab-2026-08.md` has the argument in full.

**Import discipline, inherited from api/chat/v2 and stricter.** `deepagents`
costs ~107 MB of RSS on top of LangChain's ~28 MB, against a 512 MB Render
starter instance. Nothing here may be imported at module scope by
`api/main.py`, and the router's own handlers import this package's modules
inside their function bodies rather than at module scope. Within a module
that is itself only reached lazily (`checkpointer.py` needs its base class at
class-definition time) module-scope LangGraph imports are fine — the cost is
paid on the first `/chat/deep` request either way.
`tests/api/test_chat_deep_router.py` pins the boundary in a clean subprocess:
an accidental module-scope import in `api/main.py` is a deploy-time OOM, not
a test failure.
"""
