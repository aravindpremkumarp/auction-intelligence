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

from api.neo4j_client import run_query


def list_conversations(supabase_id: str) -> list[dict]:
    rows = run_query(
        """
        MATCH (u:User {supabase_id: $sub})-[:OWNS]->(c:Conversation)
        RETURN c.id           AS id,
               c.title        AS title,
               toString(c.updated_at) AS updated_at
        ORDER BY c.updated_at DESC
        LIMIT 50
        """,
        {"sub": supabase_id},
    )
    return [
        {"id": r["id"], "title": r["title"], "updated_at": r["updated_at"]}
        for r in rows
        if r.get("id")
    ]


def get_conversation(supabase_id: str, conv_id: str) -> dict | None:
    rows = run_query(
        """
        MATCH (u:User {supabase_id: $sub})-[:OWNS]->(c:Conversation {id: $cid})
        RETURN c.id              AS id,
               c.title           AS title,
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


def upsert_conversation(
    supabase_id: str,
    conv_id: str,
    title: str,
    messages_json: str,
    api_history_json: str,
    results_json: str,
    total_count: int | None,
) -> None:
    run_query(
        """
        MATCH (u:User {supabase_id: $sub})
        MERGE (u)-[:OWNS]->(c:Conversation {id: $cid})
          ON CREATE SET c.created_at = datetime()
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
        },
    )


def delete_conversation(supabase_id: str, conv_id: str) -> None:
    run_query(
        """
        MATCH (:User {supabase_id: $sub})-[:OWNS]->(c:Conversation {id: $cid})
        DETACH DELETE c
        """,
        {"sub": supabase_id, "cid": conv_id},
    )
