"""
api/property_chats/router.py
----------------------------
`/property-chats/{property_id}` endpoints. All require a valid Supabase
access token; each chat is keyed to the authenticated user's supabase_id
and the auction's property_id.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from api.auth.dependencies import get_current_user
from api.auth.schemas import UserOut
from api.property_chats import repository as repo


router = APIRouter()


class PropertyChatUpsertIn(BaseModel):
    messages: list[Any] = Field(default_factory=list)


@router.get("/property-chats/{property_id}")
async def get_property_chat(
    property_id: str,
    user: UserOut = Depends(get_current_user),
) -> dict:
    row = repo.get_property_chat(user.id, property_id)
    if not row:
        raise HTTPException(status_code=404, detail="property chat not found")

    raw = row.get("messages_json") or ""
    try:
        messages = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        messages = []

    return {
        "property_id": row["property_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "messages": messages,
    }


@router.put("/property-chats/{property_id}", status_code=204)
async def upsert_property_chat(
    property_id: str,
    body: PropertyChatUpsertIn,
    user: UserOut = Depends(get_current_user),
) -> Response:
    repo.upsert_property_chat(
        supabase_id=user.id,
        property_id=property_id,
        messages_json=json.dumps(body.messages),
    )
    return Response(status_code=204)


@router.delete("/property-chats/{property_id}", status_code=204)
async def delete_property_chat(
    property_id: str,
    user: UserOut = Depends(get_current_user),
) -> Response:
    repo.delete_property_chat(user.id, property_id)
    return Response(status_code=204)
