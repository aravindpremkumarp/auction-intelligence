"""
api/main.py
-----------
FastAPI entry point + static UI serving.
Run with: uvicorn api.main:app --reload
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ToolCallPart,
    ToolReturnPart,
)

from api.agent import ChatDeps, agent
from api.neo4j_client import run_query

_SEARCH_TOOLS = {"search_auctions", "semantic_property_search"}

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

app = FastAPI(title="Bank Auction Intelligence API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    message_history: list[dict[str, Any]] | None = None


class ToolArtifact(BaseModel):
    tool: str
    args: dict[str, Any] | str | None = None
    result: Any = None


class ChatResponse(BaseModel):
    answer: str
    artifacts: list[ToolArtifact] = []
    message_history: list[dict[str, Any]] = []


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _extract_last_search(messages) -> dict | None:
    """Find the most recent search tool result and summarize its filters + total_count."""
    calls: dict[str, ToolCallPart] = {}
    latest: dict | None = None
    for msg in messages:
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolCallPart):
                calls[part.tool_call_id] = part
            elif isinstance(part, ToolReturnPart) and part.tool_name in _SEARCH_TOOLS:
                call = calls.get(part.tool_call_id)
                if call is None:
                    continue
                try:
                    filters = call.args_as_dict()
                except Exception:
                    filters = {}
                result = part.content
                if isinstance(result, dict) and "total_count" in result:
                    total = result["total_count"]
                elif isinstance(result, list):
                    total = len(result)
                else:
                    total = None
                latest = {"tool": part.tool_name, "filters": filters, "total_count": total}
    return latest


def _extract_artifacts(messages) -> list[ToolArtifact]:
    """Pair ToolCallPart with its matching ToolReturnPart by tool_call_id."""
    calls: dict[str, ToolCallPart] = {}
    artifacts: list[ToolArtifact] = []
    for msg in messages:
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolCallPart):
                calls[part.tool_call_id] = part
            elif isinstance(part, ToolReturnPart):
                call = calls.get(part.tool_call_id)
                if call is not None:
                    try:
                        args = call.args_as_dict()
                    except Exception:
                        args = call.args if isinstance(call.args, (dict, str)) else str(call.args)
                    artifacts.append(ToolArtifact(
                        tool=part.tool_name,
                        args=args,
                        result=part.content,
                    ))
    return artifacts


class FeedbackRequest(BaseModel):
    rating: Literal["up", "down"]
    text: str | None = None
    session_id: str
    message_index: int
    question: str
    answer: str
    artifacts: list[dict[str, Any]] | None = None
    context_turns: list[dict[str, Any]] | None = None
    user_agent: str | None = None


class FeedbackRecord(BaseModel):
    id: str
    rating: Literal["up", "down"]
    text: str | None = None
    session_id: str
    message_index: int
    question: str
    answer: str
    artifacts: list[dict[str, Any]] | None = None
    context_turns: list[dict[str, Any]] | None = None
    user_agent: str | None = None
    created_at: str
    resolved: bool = False


def _strip_artifacts(arts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not arts:
        return []
    return [{"tool": a.get("tool"), "args": a.get("args")} for a in arts]


def _strip_context_turns(turns: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Keep role, content, and tool_calls (tool+args only). Drop everything else."""
    if not turns:
        return []
    out: list[dict[str, Any]] = []
    for t in turns:
        role = t.get("role")
        if role not in ("user", "assistant"):
            continue
        entry: dict[str, Any] = {"role": role, "content": (t.get("content") or "")[:2000]}
        if role == "assistant":
            entry["tool_calls"] = _strip_artifacts(t.get("tool_calls"))
        out.append(entry)
    return out


def _feedback_row_to_record(row: dict) -> FeedbackRecord:
    f = row["f"] if "f" in row else row
    try:
        artifacts = json.loads(f.get("artifacts_json") or "[]")
    except json.JSONDecodeError:
        artifacts = []
    try:
        context_turns = json.loads(f.get("context_turns_json") or "[]")
    except json.JSONDecodeError:
        context_turns = []
    created_at = f.get("created_at")
    # neo4j DateTime → ISO string
    created_at_str = created_at.iso_format() if hasattr(created_at, "iso_format") else str(created_at)
    return FeedbackRecord(
        id=f["id"],
        rating=f["rating"],
        text=f.get("text"),
        session_id=f["session_id"],
        message_index=f["message_index"],
        question=f["question"],
        answer=f["answer"],
        artifacts=artifacts,
        context_turns=context_turns,
        user_agent=f.get("user_agent"),
        created_at=created_at_str,
        resolved=bool(f.get("resolved", False)),
    )


@app.post("/feedback")
async def submit_feedback(req: FeedbackRequest) -> dict:
    fid = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    artifacts_json = json.dumps(_strip_artifacts(req.artifacts))
    context_turns_json = json.dumps(_strip_context_turns(req.context_turns))
    answer_trimmed = (req.answer or "")[:4000]
    run_query(
        """
        CREATE (f:Feedback {
          id: $id, rating: $rating, text: $text, session_id: $session_id,
          message_index: $message_index, question: $question, answer: $answer,
          artifacts_json: $artifacts_json, context_turns_json: $context_turns_json,
          user_agent: $user_agent,
          created_at: datetime($created_at), resolved: false
        })
        RETURN f.id AS id
        """,
        {
            "id": fid,
            "rating": req.rating,
            "text": req.text,
            "session_id": req.session_id,
            "message_index": req.message_index,
            "question": req.question,
            "answer": answer_trimmed,
            "artifacts_json": artifacts_json,
            "context_turns_json": context_turns_json,
            "user_agent": req.user_agent,
            "created_at": created_at,
        },
    )
    return {"id": fid, "status": "saved"}


@app.get("/feedback/recent", response_model=list[FeedbackRecord])
async def list_feedback(
    limit: int = 50,
    unresolved_only: bool = True,
    rating: Literal["up", "down"] | None = None,
) -> list[FeedbackRecord]:
    rows = run_query(
        """
        MATCH (f:Feedback)
        WHERE ($unresolved = false OR f.resolved = false)
          AND ($rating IS NULL OR f.rating = $rating)
        RETURN f { .* } AS f
        ORDER BY f.created_at DESC
        LIMIT $limit
        """,
        {"unresolved": unresolved_only, "rating": rating, "limit": limit},
    )
    return [_feedback_row_to_record(r) for r in rows]


@app.patch("/feedback/{feedback_id}/resolve")
async def resolve_feedback(
    feedback_id: str,
    x_resolve_token: str = Header(...),
) -> dict:
    expected = os.environ.get("FEEDBACK_RESOLVE_TOKEN")
    if not expected or x_resolve_token != expected:
        raise HTTPException(status_code=401, detail="Invalid resolve token")
    rows = run_query(
        """
        MATCH (f:Feedback {id: $id})
        SET f.resolved = true, f.resolved_at = datetime()
        RETURN f.id AS id
        """,
        {"id": feedback_id},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Feedback not found")
    return {"id": feedback_id, "resolved": True}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    history = (
        ModelMessagesTypeAdapter.validate_python(req.message_history)
        if req.message_history
        else None
    )
    deps = ChatDeps(last_search=_extract_last_search(history) if history else None)
    result = await agent.run(req.message, message_history=history, deps=deps)
    return ChatResponse(
        answer=result.output,
        artifacts=_extract_artifacts(result.new_messages()),
        message_history=ModelMessagesTypeAdapter.dump_python(result.all_messages(), mode="json"),
    )


# Serve the single-page UI
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    def root() -> FileResponse:
        return FileResponse(str(WEB_DIR / "index.html"))
