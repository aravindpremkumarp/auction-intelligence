"""
api/main.py
-----------
FastAPI entry point + static UI serving.
Run with: uvicorn api.main:app --reload
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
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
