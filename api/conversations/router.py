"""
api/conversations/router.py
---------------------------
`/conversations` endpoints. All require a valid Supabase access token;
each conversation is keyed to the authenticated user's supabase_id.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from api.auth.dependencies import get_current_user
from api.auth.schemas import UserOut
from api.conversations import repository as repo


router = APIRouter()


class ConversationUpsertIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    messages: list[Any] = Field(default_factory=list)
    api_history: list[Any] | None = None
    results: list[Any] = Field(default_factory=list)
    total_count: int | None = None


@router.get("/conversations")
async def list_conversations(
    user: UserOut = Depends(get_current_user),
) -> dict:
    return {"conversations": repo.list_conversations(user.id)}


@router.get("/conversations/{conv_id}")
async def get_conversation(
    conv_id: str,
    user: UserOut = Depends(get_current_user),
) -> dict:
    row = repo.get_conversation(user.id, conv_id)
    if not row:
        raise HTTPException(status_code=404, detail="conversation not found")

    def _parse(s: str | None, fallback):
        if not s:
            return fallback
        try:
            return json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return fallback

    return {
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "messages": _parse(row.get("messages_json"), []),
        "api_history": _parse(row.get("api_history_json"), None),
        "results": _parse(row.get("results_json"), []),
        "total_count": row.get("total_count"),
    }


@router.put("/conversations/{conv_id}", status_code=204)
async def upsert_conversation(
    conv_id: str,
    body: ConversationUpsertIn,
    user: UserOut = Depends(get_current_user),
) -> Response:
    repo.upsert_conversation(
        supabase_id=user.id,
        conv_id=conv_id,
        title=body.title[:200],
        messages_json=json.dumps(body.messages),
        api_history_json=json.dumps(body.api_history) if body.api_history is not None else "",
        results_json=json.dumps(body.results),
        total_count=body.total_count,
    )
    return Response(status_code=204)


@router.delete("/conversations/{conv_id}", status_code=204)
async def delete_conversation(
    conv_id: str,
    user: UserOut = Depends(get_current_user),
) -> Response:
    repo.delete_conversation(user.id, conv_id)
    return Response(status_code=204)
