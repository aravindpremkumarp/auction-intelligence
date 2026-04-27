"""
api/property_chats/repository.py
--------------------------------
Cypher gateway for per-(user, property) chat threads
(`User -[:OWNS_PROPERTY_CHAT]-> PropertyChat`).

Uniqueness is enforced by `MERGE` on the relationship path keyed by
`property_id`, mirroring how `Conversation` upserts work — no separate
constraint required.

`messages_json` is stored as an opaque string; only the property-detail
chat UI re-hydrates it.
"""
from __future__ import annotations

from api.neo4j_client import run_query


def get_property_chat(supabase_id: str, property_id: str) -> dict | None:
    rows = run_query(
        """
        MATCH (u:User {supabase_id: $sub})-[:OWNS_PROPERTY_CHAT]->(pc:PropertyChat {property_id: $pid})
        RETURN pc.property_id    AS property_id,
               toString(pc.created_at) AS created_at,
               toString(pc.updated_at) AS updated_at,
               pc.messages_json  AS messages_json
        """,
        {"sub": supabase_id, "pid": property_id},
    )
    if not rows:
        return None
    return dict(rows[0])


def upsert_property_chat(
    supabase_id: str,
    property_id: str,
    messages_json: str,
) -> None:
    run_query(
        """
        MATCH (u:User {supabase_id: $sub})
        MERGE (u)-[:OWNS_PROPERTY_CHAT]->(pc:PropertyChat {property_id: $pid})
          ON CREATE SET pc.created_at = datetime()
        SET pc.messages_json = $messages,
            pc.updated_at    = datetime()
        """,
        {"sub": supabase_id, "pid": property_id, "messages": messages_json},
    )


def delete_property_chat(supabase_id: str, property_id: str) -> None:
    run_query(
        """
        MATCH (:User {supabase_id: $sub})-[:OWNS_PROPERTY_CHAT]->(pc:PropertyChat {property_id: $pid})
        DETACH DELETE pc
        """,
        {"sub": supabase_id, "pid": property_id},
    )
