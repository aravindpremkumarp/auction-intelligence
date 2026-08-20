"""
api/chat/deep/checkpointer.py
-----------------------------
A LangGraph `BaseCheckpointSaver` backed by Neo4j.

**Why Neo4j and not Postgres.** Every piece of application state in this repo
already lives in the graph — users, dossiers, and conversations
(`api/conversations/repository.py` stores a `Conversation` node carrying
`api_history_json`). Supabase is auth only: the backend verifies JWTs against
its JWKS and never opens a SQL connection, and there is no driver, pool or
connection string anywhere in `requirements.lock`. Adopting the official
Postgres checkpointer would mean a second datastore, a second pool and a new
credential on a 512 MB instance, to store the thing the graph already stores
next to it.

**What this is NOT.** It is not a serializer. `BaseCheckpointSaver.serde` is
`JsonPlusSerializer`, which already handles LangChain messages, datetimes and
the rest; this class only implements the storage interface around it.

**Async only.** The chat request path is `async def` throughout, so the graph
calls `aget_tuple` / `aput` / `aput_writes` / `alist`. The sync twins raise
rather than silently opening a blocking driver session inside the event loop
— a sync checkpointer call from an async graph is a bug in the caller, and it
should say so loudly instead of stalling the worker.

Shape in the graph:

    (:Conversation {id})-[:HAS_CHECKPOINT]->(:Checkpoint {
        thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
        checkpoint_b64, checkpoint_type, metadata_b64, metadata_type,
        created_at
    })-[:HAS_WRITE]->(:CheckpointWrite {
        task_id, idx, channel, value_b64, value_type
    })

Every blob is stored with the `*_type` tag its serializer returned, and read
back with that same tag. `JsonPlusSerializer` picks msgpack for most payloads
and falls back per value, so hardcoding a tag on the read side corrupts every
checkpoint — silently, because the failure surfaces as a decode error deep
inside the graph rather than at the write.

`Conversation` is MERGEd rather than MATCHed: a /lab turn can run before the
conversation row exists (the browser mints the id and saves on the first
answer), and a checkpoint that silently vanished because its parent node was
not there yet is the worst possible failure for a memory store.
"""
from __future__ import annotations

import base64
from typing import Any, AsyncIterator, Sequence

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langchain_core.runnables import RunnableConfig

from api.neo4j_client import run_query_async, run_read_query_async

#: Checkpoints kept per thread. LangGraph writes one per superstep, so a long
#: conversation accumulates them quickly and only the newest is ever read on
#: the resume path. Older ones are pruned on write, in the same query, so a
#: thread cannot grow without bound between conversations.
MAX_CHECKPOINTS_PER_THREAD = 40

#: Cap on one serialized checkpoint. A turn that somehow produced a payload
#: this large is a runaway, and storing it would make the NEXT resume load it
#: back into memory on a 512 MB instance.
MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


#: Only for rows written before the type tag was stored. There are none in any
#: deployed environment (this shipped with the feature), but a null tag must
#: degrade to a guess rather than a TypeError deep inside the graph.
_DEFAULT_TYPE = "msgpack"


def _thread_id(config: RunnableConfig) -> str:
    return str((config.get("configurable") or {}).get("thread_id") or "")


def _ns(config: RunnableConfig) -> str:
    return str((config.get("configurable") or {}).get("checkpoint_ns") or "")


def _checkpoint_id(config: RunnableConfig) -> str | None:
    value = (config.get("configurable") or {}).get("checkpoint_id")
    return str(value) if value else None


class Neo4jSaver(BaseCheckpointSaver):
    """Durable LangGraph checkpoints on the conversation graph.

    One instance is safe to share across requests: it holds no connection of
    its own, deferring to `api/neo4j_client`'s pooled driver exactly like
    every other repository in this codebase.
    """

    # ── read ────────────────────────────────────────────────────────────────

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = _thread_id(config)
        if not thread_id:
            return None
        ns = _ns(config)
        wanted = _checkpoint_id(config)

        # Two shapes: resume-latest (no checkpoint_id) and time-travel to a
        # specific one. Same query with a null-guard rather than two, so the
        # ordering rule that decides "latest" exists in one place.
        rows = await run_read_query_async(
            """
            MATCH (c:Checkpoint {thread_id: $tid, checkpoint_ns: $ns})
            WHERE $cid IS NULL OR c.checkpoint_id = $cid
            RETURN c.checkpoint_id        AS checkpoint_id,
                   c.parent_checkpoint_id AS parent_checkpoint_id,
                   c.checkpoint_b64       AS checkpoint_b64,
                   c.checkpoint_type      AS checkpoint_type,
                   c.metadata_b64         AS metadata_b64,
                   c.metadata_type        AS metadata_type
            ORDER BY c.created_at DESC, c.checkpoint_id DESC
            LIMIT 1
            """,
            {"tid": thread_id, "ns": ns, "cid": wanted},
            timeout=15.0,
            max_rows=1,
        )
        if not rows:
            return None
        return await self._to_tuple(thread_id, ns, rows[0])

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,   # noqa: A002 - interface name
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        thread_id = _thread_id(config or {})
        if not thread_id:
            return
        rows = await run_read_query_async(
            """
            MATCH (c:Checkpoint {thread_id: $tid, checkpoint_ns: $ns})
            WHERE $before IS NULL OR c.checkpoint_id < $before
            RETURN c.checkpoint_id        AS checkpoint_id,
                   c.parent_checkpoint_id AS parent_checkpoint_id,
                   c.checkpoint_b64       AS checkpoint_b64,
                   c.checkpoint_type      AS checkpoint_type,
                   c.metadata_b64         AS metadata_b64,
                   c.metadata_type        AS metadata_type
            ORDER BY c.created_at DESC, c.checkpoint_id DESC
            LIMIT $limit
            """,
            {
                "tid": thread_id,
                "ns": _ns(config or {}),
                "before": _checkpoint_id(before) if before else None,
                "limit": int(limit or MAX_CHECKPOINTS_PER_THREAD),
            },
            timeout=15.0,
            max_rows=int(limit or MAX_CHECKPOINTS_PER_THREAD),
        )
        for row in rows:
            # `filter` matches on metadata, which is serialized here, so it is
            # applied after load rather than in Cypher.
            tup = await self._to_tuple(thread_id, _ns(config or {}), row,
                                       with_writes=False)
            if filter and not _metadata_matches(tup.metadata, filter):
                continue
            yield tup

    async def _to_tuple(self, thread_id: str, ns: str, row: dict,
                        *, with_writes: bool = True) -> CheckpointTuple:
        # The type tag is the serializer's, not ours — JsonPlusSerializer
        # picks msgpack for most payloads and falls back per value. Storing
        # the tag it returned and handing the same one back is the whole
        # contract; assuming "json" here silently corrupts every checkpoint.
        checkpoint: Checkpoint = self.serde.loads_typed(
            (row.get("checkpoint_type") or _DEFAULT_TYPE,
             _unb64(row["checkpoint_b64"]))
        )
        metadata: CheckpointMetadata = self.serde.loads_typed(
            (row.get("metadata_type") or _DEFAULT_TYPE,
             _unb64(row["metadata_b64"]))
        )
        checkpoint_id = row["checkpoint_id"]
        parent_id = row.get("parent_checkpoint_id")
        config: RunnableConfig = {"configurable": {
            "thread_id": thread_id, "checkpoint_ns": ns,
            "checkpoint_id": checkpoint_id,
        }}
        parent_config: RunnableConfig | None = None
        if parent_id:
            parent_config = {"configurable": {
                "thread_id": thread_id, "checkpoint_ns": ns,
                "checkpoint_id": parent_id,
            }}
        writes = await self._pending_writes(thread_id, ns, checkpoint_id) \
            if with_writes else []
        return CheckpointTuple(
            config=config, checkpoint=checkpoint, metadata=metadata,
            parent_config=parent_config, pending_writes=writes,
        )

    async def _pending_writes(self, thread_id: str, ns: str,
                              checkpoint_id: str) -> list[tuple[str, str, Any]]:
        rows = await run_read_query_async(
            """
            MATCH (c:Checkpoint {thread_id: $tid, checkpoint_ns: $ns,
                                 checkpoint_id: $cid})-[:HAS_WRITE]->(w:CheckpointWrite)
            RETURN w.task_id AS task_id, w.channel AS channel,
                   w.value_b64 AS value_b64, w.value_type AS value_type
            ORDER BY w.task_id, w.idx
            """,
            {"tid": thread_id, "ns": ns, "cid": checkpoint_id},
            timeout=15.0,
            max_rows=500,
        )
        return [
            (r["task_id"], r["channel"],
             self.serde.loads_typed((r.get("value_type") or _DEFAULT_TYPE,
                                     _unb64(r["value_b64"]))))
            for r in rows
        ]

    # ── write ───────────────────────────────────────────────────────────────

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = _thread_id(config)
        if not thread_id:
            raise ValueError("Neo4jSaver.aput: config carries no thread_id")
        ns = _ns(config)
        checkpoint_id = str(checkpoint["id"])
        parent_id = _checkpoint_id(config)

        checkpoint_type, checkpoint_bytes = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_bytes = self.serde.dumps_typed(metadata)
        if len(checkpoint_bytes) > MAX_CHECKPOINT_BYTES:
            raise ValueError(
                f"checkpoint is {len(checkpoint_bytes)} bytes, over the "
                f"{MAX_CHECKPOINT_BYTES} cap — refusing to store a payload "
                "the next resume would have to load back"
            )

        # MERGE the Conversation: on /lab the first turn can precede the
        # conversation row the browser saves after the answer lands.
        # `ON CREATE` only, so this never overwrites a real title.
        await run_query_async(
            """
            MERGE (conv:Conversation {id: $tid})
              ON CREATE SET conv.created_at = datetime(),
                            conv.updated_at = datetime()
            MERGE (c:Checkpoint {thread_id: $tid, checkpoint_ns: $ns,
                                 checkpoint_id: $cid})
              ON CREATE SET c.created_at = datetime()
            SET c.parent_checkpoint_id = $parent,
                c.checkpoint_b64 = $checkpoint,
                c.checkpoint_type = $checkpoint_type,
                c.metadata_b64 = $metadata,
                c.metadata_type = $metadata_type
            MERGE (conv)-[:HAS_CHECKPOINT]->(c)
            WITH conv
            // Prune in the same write: keep the newest N, detach the rest
            // with their writes. A thread must not grow without bound.
            MATCH (conv)-[:HAS_CHECKPOINT]->(old:Checkpoint)
            WITH conv, old ORDER BY old.created_at DESC, old.checkpoint_id DESC
            SKIP $keep
            OPTIONAL MATCH (old)-[:HAS_WRITE]->(w:CheckpointWrite)
            DETACH DELETE old, w
            """,
            {
                "tid": thread_id, "ns": ns, "cid": checkpoint_id,
                "parent": parent_id,
                "checkpoint": _b64(checkpoint_bytes),
                "checkpoint_type": checkpoint_type,
                "metadata": _b64(metadata_bytes),
                "metadata_type": metadata_type,
                "keep": MAX_CHECKPOINTS_PER_THREAD,
            },
        )
        return {"configurable": {
            "thread_id": thread_id, "checkpoint_ns": ns,
            "checkpoint_id": checkpoint_id,
        }}

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = _thread_id(config)
        checkpoint_id = _checkpoint_id(config)
        if not thread_id or not checkpoint_id or not writes:
            return
        rows = []
        for idx, (channel, value) in enumerate(writes):
            value_type, value_bytes = self.serde.dumps_typed(value)
            rows.append({"idx": idx, "channel": channel,
                         "value_b64": _b64(value_bytes),
                         "value_type": value_type})
        await run_query_async(
            """
            MATCH (c:Checkpoint {thread_id: $tid, checkpoint_ns: $ns,
                                 checkpoint_id: $cid})
            UNWIND $rows AS row
            MERGE (w:CheckpointWrite {task_id: $task, idx: row.idx})
            SET w.channel = row.channel, w.value_b64 = row.value_b64,
                w.value_type = row.value_type
            MERGE (c)-[:HAS_WRITE]->(w)
            """,
            {"tid": thread_id, "ns": _ns(config), "cid": checkpoint_id,
             "task": task_id, "rows": rows},
        )

    async def adelete_thread(self, thread_id: str) -> None:
        """Drop every checkpoint for a thread.

        Called when the user starts a new thread or deletes a conversation —
        the server-side twin of the client-side state clearing that the tiered
        loop needs `apiChatScope = null` for.
        """
        await run_query_async(
            """
            MATCH (c:Checkpoint {thread_id: $tid})
            OPTIONAL MATCH (c)-[:HAS_WRITE]->(w:CheckpointWrite)
            DETACH DELETE c, w
            """,
            {"tid": thread_id},
        )

    # ── sync twins ──────────────────────────────────────────────────────────
    #
    # Deliberately unimplemented. The request path is async; a sync call here
    # would mean a blocking driver session inside the event loop, which on a
    # 0.5-vCPU instance stalls every other in-flight request. Failing loudly
    # is the correct behaviour.

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        raise NotImplementedError(_SYNC_MSG)

    def list(self, config, **kwargs):  # noqa: A003 - interface name
        raise NotImplementedError(_SYNC_MSG)

    def put(self, config, checkpoint, metadata, new_versions):
        raise NotImplementedError(_SYNC_MSG)

    def put_writes(self, config, writes, task_id, task_path=""):
        raise NotImplementedError(_SYNC_MSG)


_SYNC_MSG = (
    "Neo4jSaver is async-only — invoke the graph with ainvoke/astream. A sync "
    "checkpointer call from the async request path would block the event loop."
)


def _metadata_matches(metadata: CheckpointMetadata, filter: dict) -> bool:  # noqa: A002
    return all(metadata.get(key) == value for key, value in filter.items())


#: Index DDL. Run by `scripts/`-style migration alongside the other
#: constraints; kept here so the shape and its index live in one file.
INDEX_STATEMENTS = (
    "CREATE INDEX checkpoint_thread IF NOT EXISTS "
    "FOR (c:Checkpoint) ON (c.thread_id, c.checkpoint_ns, c.checkpoint_id)",
    "CREATE INDEX checkpoint_recency IF NOT EXISTS "
    "FOR (c:Checkpoint) ON (c.thread_id, c.created_at)",
)
