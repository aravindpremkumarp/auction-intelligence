"""
api/feedback/router.py
----------------------
Feedback capture + admin/automation review endpoints. Extracted from
api/main.py. The models and trimming helpers (`_strip_artifacts`,
`_strip_context_turns`) keep stored payloads small — we persist tool/arg
breadcrumbs and truncated prose, never full result rows.
"""
from __future__ import annotations

import json
import os
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from api.auth import get_current_admin, get_optional_user
from api.auth.schemas import UserOut
from api.neo4j_client import run_query

router = APIRouter()


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
    property_id: str | None = None


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
    property_id: str | None = None
    created_at: str
    resolved: bool = False
    resolved_at: str | None = None


def _resolve_token_ok(supplied: str | None) -> bool:
    """Constant-time check of the shared automation token."""
    expected = os.environ.get("FEEDBACK_RESOLVE_TOKEN")
    if not expected or not supplied:
        return False
    return secrets.compare_digest(supplied, expected)


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
        property_id=f.get("property_id"),
        created_at=created_at_str,
        resolved=bool(f.get("resolved", False)),
        resolved_at=resolved_at_str,
    )


@router.post("/feedback")
def submit_feedback(
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
          property_id: $property_id,
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
            "property_id": req.property_id,
            "created_at": created_at,
        },
    )
    return {"id": fid, "status": "saved"}


@router.get("/feedback/recent", response_model=list[FeedbackRecord])
def list_feedback(
    limit: int = 50,
    unresolved_only: bool = True,
    rating: Literal["up", "down"] | None = None,
    kind: Literal["message", "general"] | None = None,
    x_resolve_token: str | None = Header(default=None),
    user: UserOut | None = Depends(get_optional_user),
) -> list[FeedbackRecord]:
    """List feedback for triage. Records carry users' chat context
    (`context_turns`, `session_id`, `user_agent`), so reads require either an
    admin JWT or the shared `X-Resolve-Token` used by the sync workflow."""
    admin_ok = user is not None and user.role == "admin"
    if not (_resolve_token_ok(x_resolve_token) or admin_ok):
        raise HTTPException(status_code=401, detail="Invalid feedback credentials")
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


@router.patch("/feedback/{feedback_id}/resolve")
def resolve_feedback(
    feedback_id: str,
    x_resolve_token: str | None = Header(default=None),
    user: UserOut | None = Depends(get_optional_user),
) -> dict:
    """Mark a feedback item as resolved.

    Accepts either a shared `X-Resolve-Token` (used by the GitHub
    `resolve-feedback` workflow) or a Supabase JWT from an admin user. When
    an admin closes the item we also persist `resolved_by` / `resolved_by_email`
    for audit.
    """
    token_ok = _resolve_token_ok(x_resolve_token)
    admin_ok = user is not None and user.role == "admin"
    if not (token_ok or admin_ok):
        raise HTTPException(status_code=401, detail="Invalid resolve credentials")

    params: dict[str, Any] = {"id": feedback_id}
    set_clause = "SET f.resolved = true, f.resolved_at = datetime()"
    if admin_ok and user is not None:
        set_clause += ", f.resolved_by = $resolved_by, f.resolved_by_email = $resolved_by_email"
        params["resolved_by"] = user.id
        params["resolved_by_email"] = user.email

    rows = run_query(
        f"""
        MATCH (f:Feedback {{id: $id}})
        {set_clause}
        RETURN f.id AS id
        """,
        params,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Feedback not found")
    return {"id": feedback_id, "resolved": True}


@router.get("/admin/feedback", response_model=list[FeedbackRecord])
def list_admin_feedback(
    limit: int = 100,
    unresolved_only: bool = True,
    _admin: UserOut = Depends(get_current_admin),
) -> list[FeedbackRecord]:
    rows = run_query(
        """
        MATCH (f:Feedback)
        WHERE ($unresolved = false OR f.resolved = false)
        RETURN f { .* } AS f
        ORDER BY f.created_at DESC
        LIMIT $limit
        """,
        {"unresolved": unresolved_only, "limit": limit},
    )
    return [_feedback_row_to_record(r) for r in rows]
