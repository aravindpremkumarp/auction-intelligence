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

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ToolCallPart,
    ToolReturnPart,
)
from slowapi.errors import RateLimitExceeded

from api.agent import ChatDeps, agent
from api.auth import get_optional_user, router as auth_router
from api.auth.rate_limit import limiter
from api.auth.schemas import UserOut
from api.neo4j_client import run_query
from api.tools.cypher_tools import get_auction_detail

_SEARCH_TOOLS = {"search_auctions", "semantic_property_search"}

# Args that describe scope we want to carry across turns. Excludes output
# controls (limit, order_by) and aggregate knobs — those don't narrow the
# user's target set, they just shape the current call.
_CARRY_FORWARD_FILTER_KEYS = {
    "min_price", "max_price",
    "city", "area",
    "property_type", "asset_category",
    "bank",
    "starts_after", "starts_before",
}

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

app = FastAPI(title="Bank Auction Intelligence API", version="0.1.0")


def _cors_allow_list() -> list[str]:
    base = os.environ.get("APP_BASE_URL", "").strip()
    env = os.environ.get("APP_ENV", "prod").lower()
    if env in {"dev", "test"}:
        return ["*"]
    origins = {"http://localhost:5173", "http://localhost:3000"}
    if base:
        origins.add(base.rstrip("/"))
    return sorted(origins)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_list(),
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
)

# Rate limiter (slowapi) — shared across auth + anonymous chat throttles.
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": "rate limit exceeded"})


if os.environ.get("AUTH_ENABLED", "true").lower() != "false":
    app.include_router(auth_router)


_GATED_MODES = {"deep-research", "report"}

# Simple in-memory hourly counter for anonymous /chat. slowapi's decorator can't
# see Depends-provided state, so we enforce this manually only when user=None.
_ANON_CHAT_MAX_PER_HOUR = 10
_anon_chat_hits: dict[str, list[float]] = {}


def _enforce_anon_chat_limit(request: Request) -> None:
    if os.environ.get("RATELIMIT_DISABLED", "").lower() in {"1", "true", "yes"}:
        return
    import time
    now = time.time()
    window = 3600.0
    ip = request.client.host if request.client else "unknown"
    hits = [t for t in _anon_chat_hits.get(ip, []) if now - t < window]
    if len(hits) >= _ANON_CHAT_MAX_PER_HOUR:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    hits.append(now)
    _anon_chat_hits[ip] = hits


class ChatRequest(BaseModel):
    message: str
    message_history: list[dict[str, Any]] | None = None
    mode: str | None = None


# Whitelist of modes the agent will overlay. Each maps to a modes/<id>.md file.
# Keeping this explicit prevents arbitrary file reads from the modes/ dir.
_AVAILABLE_MODES: list[dict[str, Any]] = [
    {
        "id": "ask",
        "label": "Ask",
        "description": "Free-form Q&A over the Neo4j graph (default).",
        "examples": [
            "Residential auctions in Chennai under 30 lakhs",
            "What is the price range in Kanchipuram?",
            "Show auctions with deadline in the next 7 days",
            "How many banks have auctions in Chennai?",
            "Which borrowers have more than 3 properties?",
            "List all cities in the database",
        ],
    },
    {
        "id": "deep-research",
        "label": "Deep research",
        "description": "7-step due-diligence workflow on one auction_id.",
        "examples": [
            "Deep research on auction AUC-12345",
        ],
    },
    {
        "id": "compare",
        "label": "Compare",
        "description": "Side-by-side comparison of 2–5 auctions.",
        "examples": [
            "Compare AUC-12345 and AUC-67890",
        ],
    },
    {
        "id": "report",
        "label": "Personalized report",
        "description": "Investment report tuned to an investor profile.",
        "examples": [
            "Report on AUC-12345 for a conservative investor under 50 lakhs",
        ],
    },
]


class ToolArtifact(BaseModel):
    tool: str
    args: dict[str, Any] | str | None = None
    result: Any = None
    # UI-only row overflow from search tools. Populated when a search's
    # `total_count` exceeds the model-visible `limit` — lets the right-side
    # artifacts panel render every match without inflating the LLM's
    # context. Never included in `result` (which is what the model sees on
    # follow-up turns).
    ui_rows: list[dict[str, Any]] | None = None


class ChatResponse(BaseModel):
    answer: str
    artifacts: list[ToolArtifact] = []
    message_history: list[dict[str, Any]] = []


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/deep")
def health_deep() -> dict:
    """Extended health check: verifies Neo4j connectivity, counts the main
    node label, and confirms the vector index exists. Used by monitoring
    and during PR reviews to catch environment drift."""
    checks: dict[str, Any] = {"status": "ok", "errors": []}
    try:
        rows = run_query("MATCH (a:AuctionProperty) RETURN count(a) AS n")
        checks["auction_count"] = rows[0]["n"] if rows else 0
    except Exception as e:
        checks["errors"].append(f"neo4j: {e!r}")
    try:
        idx = run_query(
            "SHOW INDEXES YIELD name, type WHERE name = 'property_desc_idx' "
            "RETURN name, type"
        )
        checks["vector_index"] = idx[0] if idx else None
    except Exception as e:
        checks["errors"].append(f"vector_index: {e!r}")
    if checks["errors"]:
        checks["status"] = "degraded"
    return checks


@app.get("/modes")
def list_modes() -> dict:
    """Mode registry consumed by the web UI to render the mode selector and
    suggestion chips. Mirrors the career-ops pattern of surfacing each
    markdown mode file as a user-facing entry point."""
    return {"modes": _AVAILABLE_MODES}


def _extract_active_filters(messages) -> tuple[dict, int | None]:
    """Walk the message history and merge every prior search tool's scope
    filters into a single rolling dict — the "active scope" of the
    conversation so far.

    Rules:
      * Later calls overwrite earlier ones for the same key, so the latest
        user narrowing wins.
      * Keys not in `_CARRY_FORWARD_FILTER_KEYS` (e.g. `limit`, `order_by`,
        `aggregations`) are ignored — they're per-call, not scope.
      * A call that passes an explicit `None` for a key clears that key —
        the model is effectively telling us it dropped that scope.
      * Non-search tool calls don't affect the scope.

    Returns (filters, last_total_count). `last_total_count` is the
    total_count of the most recent search for context.
    """
    calls: dict[str, ToolCallPart] = {}
    filters: dict = {}
    last_total: int | None = None
    for msg in messages:
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolCallPart):
                calls[part.tool_call_id] = part
            elif isinstance(part, ToolReturnPart) and part.tool_name in _SEARCH_TOOLS:
                call = calls.get(part.tool_call_id)
                if call is None:
                    continue
                try:
                    args = call.args_as_dict()
                except Exception:
                    args = {}
                for key in _CARRY_FORWARD_FILTER_KEYS:
                    if key not in args:
                        continue
                    val = args[key]
                    if val is None:
                        filters.pop(key, None)
                    else:
                        filters[key] = val
                result = part.content
                if isinstance(result, dict) and "total_count" in result:
                    last_total = result["total_count"]
                elif isinstance(result, list):
                    last_total = len(result)
    return filters, last_total


def _split_ui_rows(result: Any) -> tuple[Any, list[dict[str, Any]] | None]:
    """Pop the UI-only overflow from a search-tool result.

    `search_auctions` returns `_ui_results` when total_count exceeds the
    model-visible `limit`. That list belongs in the HTTP response so the
    right-side panel can render every match — but it must NOT flow back
    into the LLM's message history on the next turn, or we defeat the
    whole point of the split. We copy the dict (so we don't mutate the
    in-memory ToolReturnPart content), pop `_ui_results`, and return the
    trimmed copy for the LLM + the raw list for the UI.
    """
    if not isinstance(result, dict) or "_ui_results" not in result:
        return result, None
    trimmed = {k: v for k, v in result.items() if k != "_ui_results"}
    ui_rows = result.get("_ui_results")
    if not isinstance(ui_rows, list):
        ui_rows = None
    return trimmed, ui_rows


def _extract_artifacts(messages) -> list[ToolArtifact]:
    """Pair ToolCallPart with its matching ToolReturnPart by tool_call_id.

    When a search tool returns `_ui_results`, move those rows onto the
    artifact's `ui_rows` field and strip them from `result`. This keeps
    the UI payload rich while the LLM-facing `result` stays lean.
    """
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
                    result, ui_rows = _split_ui_rows(part.content)
                    artifacts.append(ToolArtifact(
                        tool=part.tool_name,
                        args=args,
                        result=result,
                        ui_rows=ui_rows,
                    ))
    return artifacts


def _strip_ui_rows_from_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove `_ui_results` from any ToolReturnPart content in a dumped
    message history. Prevents the UI overflow from ballooning the LLM's
    context on the next turn when the client echoes history back.
    """
    for msg in history:
        for part in msg.get("parts", []):
            if part.get("part_kind") != "tool-return":
                continue
            content = part.get("content")
            if isinstance(content, dict) and "_ui_results" in content:
                content.pop("_ui_results", None)
    return history


class FeedbackRequest(BaseModel):
    kind: Literal["message", "general"] = "message"
    rating: Literal["up", "down"] | None = None
    text: str | None = None
    session_id: str
    message_index: int = -1
    question: str = ""
    answer: str = ""
    artifacts: list[dict[str, Any]] | None = None
    context_turns: list[dict[str, Any]] | None = None
    user_agent: str | None = None
    page_url: str | None = None


class FeedbackRecord(BaseModel):
    id: str
    kind: Literal["message", "general"] = "message"
    rating: Literal["up", "down"] | None = None
    text: str | None = None
    session_id: str
    message_index: int
    question: str
    answer: str
    artifacts: list[dict[str, Any]] | None = None
    context_turns: list[dict[str, Any]] | None = None
    user_agent: str | None = None
    page_url: str | None = None
    created_at: str
    resolved: bool = False
    resolved_at: str | None = None


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
    resolved_at = f.get("resolved_at")
    resolved_at_str: str | None
    if resolved_at is None:
        resolved_at_str = None
    else:
        resolved_at_str = resolved_at.iso_format() if hasattr(resolved_at, "iso_format") else str(resolved_at)
    return FeedbackRecord(
        id=f["id"],
        kind=f.get("kind") or "message",
        rating=f.get("rating"),
        text=f.get("text"),
        session_id=f["session_id"],
        message_index=f["message_index"],
        question=f.get("question") or "",
        answer=f.get("answer") or "",
        artifacts=artifacts,
        context_turns=context_turns,
        user_agent=f.get("user_agent"),
        page_url=f.get("page_url"),
        created_at=created_at_str,
        resolved=bool(f.get("resolved", False)),
        resolved_at=resolved_at_str,
    )


@app.post("/feedback")
async def submit_feedback(
    req: FeedbackRequest,
    user: UserOut | None = Depends(get_optional_user),
) -> dict:
    if req.kind == "general" and not (req.text and req.text.strip()) and req.rating is None:
        raise HTTPException(status_code=400, detail="General feedback requires a rating or text.")
    fid = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    artifacts_json = json.dumps(_strip_artifacts(req.artifacts))
    context_turns_json = json.dumps(_strip_context_turns(req.context_turns))
    answer_trimmed = (req.answer or "")[:4000]
    run_query(
        """
        CREATE (f:Feedback {
          id: $id, kind: $kind, rating: $rating, text: $text, session_id: $session_id,
          message_index: $message_index, question: $question, answer: $answer,
          artifacts_json: $artifacts_json, context_turns_json: $context_turns_json,
          user_agent: $user_agent, page_url: $page_url, user_id: $user_id,
          created_at: datetime($created_at), resolved: false
        })
        RETURN f.id AS id
        """,
        {
            "id": fid,
            "kind": req.kind,
            "rating": req.rating,
            "text": req.text,
            "session_id": req.session_id,
            "message_index": req.message_index,
            "question": req.question,
            "answer": answer_trimmed,
            "artifacts_json": artifacts_json,
            "context_turns_json": context_turns_json,
            "user_agent": req.user_agent,
            "page_url": req.page_url,
            "user_id": user.id if user else None,
            "created_at": created_at,
        },
    )
    return {"id": fid, "status": "saved"}


@app.get("/feedback/recent", response_model=list[FeedbackRecord])
async def list_feedback(
    limit: int = 50,
    unresolved_only: bool = True,
    rating: Literal["up", "down"] | None = None,
    kind: Literal["message", "general"] | None = None,
) -> list[FeedbackRecord]:
    rows = run_query(
        """
        MATCH (f:Feedback)
        WHERE ($unresolved = false OR f.resolved = false)
          AND ($rating IS NULL OR f.rating = $rating)
          AND ($kind IS NULL OR coalesce(f.kind, 'message') = $kind)
        RETURN f { .* } AS f
        ORDER BY f.created_at DESC
        LIMIT $limit
        """,
        {"unresolved": unresolved_only, "rating": rating, "kind": kind, "limit": limit},
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


@app.get("/auction/{auction_id}")
def auction_detail(auction_id: str) -> dict:
    detail = get_auction_detail(auction_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Auction not found")
    return detail


@app.post("/chat", response_model=ChatResponse)
async def chat(
    request: Request,
    req: ChatRequest,
    user: UserOut | None = Depends(get_optional_user),
) -> ChatResponse:
    mode = req.mode
    if mode in _GATED_MODES and (user is None or not user.email_verified):
        raise HTTPException(status_code=401, detail="login required for this mode")
    if user is None:
        _enforce_anon_chat_limit(request)
    history = (
        ModelMessagesTypeAdapter.validate_python(req.message_history)
        if req.message_history
        else None
    )
    if mode:
        valid_ids = {m["id"] for m in _AVAILABLE_MODES}
        if mode not in valid_ids or mode == "ask":
            # Unknown mode or the default "ask" sentinel — don't overlay anything.
            mode = None
    if history:
        active_filters, last_total = _extract_active_filters(history)
    else:
        active_filters, last_total = {}, None
    deps = ChatDeps(
        active_filters=active_filters or None,
        last_total_count=last_total,
        mode=mode,
    )
    result = await agent.run(req.message, message_history=history, deps=deps)
    dumped_history = ModelMessagesTypeAdapter.dump_python(result.all_messages(), mode="json")
    return ChatResponse(
        answer=result.output,
        artifacts=_extract_artifacts(result.new_messages()),
        # Strip `_ui_results` from the history echoed back to the client —
        # otherwise the client ships it back on the next /chat turn and it
        # re-enters the LLM's context, defeating the UI/LLM split.
        message_history=_strip_ui_rows_from_history(dumped_history),
    )


# Serve the single-page UI
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    def root() -> FileResponse:
        return FileResponse(str(WEB_DIR / "index.html"))

    @app.get("/auth.js")
    def auth_js() -> FileResponse:
        return FileResponse(str(WEB_DIR / "auth.js"), media_type="application/javascript")

    @app.get("/admin")
    def admin_page() -> FileResponse:
        return FileResponse(str(WEB_DIR / "admin.html"))
