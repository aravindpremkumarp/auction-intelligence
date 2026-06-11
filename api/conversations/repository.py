"""
api/conversations/repository.py
-------------------------------
Cypher gateway for per-user chat conversations
(`User -[:OWNS]-> Conversation`).

The user node is keyed by `supabase_id` (see api/auth/repository).
Conversation IDs are minted client-side (UUID); upsert handles
create-or-update so the first turn doesn't need a separate POST.

JSON-shaped fields (`messages`, `api_history`, `results`) are stored
as opaque strings — neither the LLM nor any other backend code
inspects them; only the chat UI re-hydrates them.
"""
from __future__ import annotations

from api.neo4j_client import run_query_async


async def list_conversations(supabase_id: str) -> list[dict]:
    rows = await run_query_async(
        """
        MATCH (u:User {supabase_id: $sub})-[:OWNS]->(c:Conversation)
        RETURN c.id           AS id,
               c.title        AS title,
               c.property_id  AS property_id,
               toString(c.updated_at) AS updated_at
        ORDER BY c.updated_at DESC
        LIMIT 50
        """,
        {"sub": supabase_id},
    )
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "property_id": r.get("property_id"),
            "updated_at": r["updated_at"],
        }
        for r in rows
        if r.get("id")
    ]


async def list_conversations_for_property(supabase_id: str, property_id: str) -> list[dict]:
    rows = await run_query_async(
        """
        MATCH (u:User {supabase_id: $sub})-[:OWNS]->(c:Conversation {property_id: $pid})
        RETURN c.id           AS id,
               c.title        AS title,
               c.property_id  AS property_id,
               toString(c.updated_at) AS updated_at
        ORDER BY c.updated_at DESC
        LIMIT 50
        """,
        {"sub": supabase_id, "pid": property_id},
    )
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "property_id": r.get("property_id"),
            "updated_at": r["updated_at"],
        }
        for r in rows
        if r.get("id")
    ]


async def get_conversation(supabase_id: str, conv_id: str) -> dict | None:
    rows = await run_query_async(
        """
        MATCH (u:User {supabase_id: $sub})-[:OWNS]->(c:Conversation {id: $cid})
        RETURN c.id              AS id,
               c.title           AS title,
               c.property_id     AS property_id,
               toString(c.created_at) AS created_at,
               toString(c.updated_at) AS updated_at,
               c.messages_json   AS messages_json,
               c.api_history_json AS api_history_json,
               c.results_json    AS results_json,
               c.total_count     AS total_count
        """,
        {"sub": supabase_id, "cid": conv_id},
    )
    if not rows:
        return None
    return dict(rows[0])


async def upsert_conversation(
    supabase_id: str,
    conv_id: str,
    title: str,
    messages_json: str,
    api_history_json: str,
    results_json: str,
    total_count: int | None,
    property_id: str | None = None,
) -> None:
    # property_id is set ON CREATE only — once a chat is bound to a property
    # (or is unbound, for search chats), that linkage is permanent.
    await run_query_async(
        """
        MATCH (u:User {supabase_id: $sub})
        MERGE (u)-[:OWNS]->(c:Conversation {id: $cid})
          ON CREATE SET c.created_at = datetime(),
                        c.property_id = $property_id
        SET c.title             = $title,
            c.messages_json     = $messages,
            c.api_history_json  = $api_history,
            c.results_json      = $results,
            c.total_count       = $total,
            c.updated_at        = datetime()
        """,
        {
            "sub": supabase_id,
            "cid": conv_id,
            "title": title,
            "messages": messages_json,
            "api_history": api_history_json,
            "results": results_json,
            "total": total_count,
            "property_id": property_id,
        },
    )


async def delete_conversation(supabase_id: str, conv_id: str) -> None:
    await run_query_async(
        """
        MATCH (:User {supabase_id: $sub})-[:OWNS]->(c:Conversation {id: $cid})
        DETACH DELETE c
        """,
        {"sub": supabase_id, "cid": conv_id},
    )
