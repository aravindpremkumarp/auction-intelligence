"""
tests/api/test_chat_deep_checkpointer.py
----------------------------------------
The Neo4j-backed LangGraph checkpointer, exercised with a fake graph.

`api/neo4j_client`'s two async helpers are stubbed with an in-memory store
that records the Cypher it was handed, so these run with no database. What is
worth pinning is the storage contract — a checkpoint written must come back
byte-identical, the sync methods must refuse rather than block the event
loop, and the bounds must actually bound — because a memory store that
silently loses or silently grows is worse than no memory store at all.
"""
from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.base import empty_checkpoint

from api.chat.deep import checkpointer as CP


class _FakeGraph:
    """Just enough Cypher semantics for the checkpointer's four queries.

    Deliberately not a Cypher engine: it pattern-matches on the distinctive
    clause of each query the module issues. If a query changes shape, the test
    fails loudly rather than quietly matching the wrong branch.
    """

    def __init__(self):
        self.checkpoints: dict[tuple, dict] = {}
        self.writes: list[dict] = []
        self.seen: list[str] = []

    async def write(self, cypher: str, params: dict | None = None):
        params = params or {}
        self.seen.append(cypher)
        if "MERGE (c:Checkpoint" in cypher:
            key = (params["tid"], params["ns"], params["cid"])
            self.checkpoints[key] = {
                "checkpoint_id": params["cid"],
                "parent_checkpoint_id": params["parent"],
                "checkpoint_b64": params["checkpoint"],
                "metadata_b64": params["metadata"],
                "order": len(self.checkpoints),
            }
            return []
        if "MERGE (w:CheckpointWrite" in cypher:
            for row in params["rows"]:
                self.writes.append({"cid": params["cid"], "task_id": params["task"],
                                    **row})
            return []
        if "DETACH DELETE c, w" in cypher:
            tid = params["tid"]
            self.checkpoints = {k: v for k, v in self.checkpoints.items()
                                if k[0] != tid}
            return []
        raise AssertionError(f"unexpected write query: {cypher[:80]}")

    async def read(self, cypher: str, params=None, timeout=None, max_rows=None):
        params = params or {}
        self.seen.append(cypher)
        if "HAS_WRITE]->(w:CheckpointWrite)" in cypher:
            return [w for w in self.writes if w["cid"] == params["cid"]]
        if "MATCH (c:Checkpoint" in cypher:
            rows = [
                v for k, v in self.checkpoints.items()
                if k[0] == params["tid"] and k[1] == params["ns"]
                and (params.get("cid") is None
                     or v["checkpoint_id"] == params["cid"])
            ]
            rows.sort(key=lambda r: r["order"], reverse=True)
            limit = params.get("limit", 1)
            return rows[:limit]
        raise AssertionError(f"unexpected read query: {cypher[:80]}")


@pytest.fixture
def graph(monkeypatch):
    fake = _FakeGraph()
    monkeypatch.setattr(CP, "run_query_async", fake.write)
    monkeypatch.setattr(CP, "run_read_query_async", fake.read)
    return fake


def _config(thread_id="conv-1", checkpoint_id=None):
    configurable = {"thread_id": thread_id, "checkpoint_ns": ""}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


# ── the round trip ──────────────────────────────────────────────────────────

def test_checkpoint_round_trips(graph):
    """The whole point: what goes in comes back out identical."""
    saver = CP.Neo4jSaver()
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": ["hello"]}

    async def go():
        out = await saver.aput(_config(), checkpoint, {"source": "loop"}, {})
        assert out["configurable"]["checkpoint_id"] == checkpoint["id"]
        return await saver.aget_tuple(_config())

    tup = asyncio.run(go())
    assert tup is not None
    assert tup.checkpoint["id"] == checkpoint["id"]
    assert tup.checkpoint["channel_values"] == {"messages": ["hello"]}
    assert tup.metadata["source"] == "loop"


def test_get_tuple_returns_the_newest_when_no_id_is_named(graph):
    """Resume-latest is the hot path — every ordinary turn takes it."""
    saver = CP.Neo4jSaver()

    async def go():
        for text in ("first", "second", "third"):
            cp = empty_checkpoint()
            cp["channel_values"] = {"messages": [text]}
            await saver.aput(_config(), cp, {}, {})
        return await saver.aget_tuple(_config())

    tup = asyncio.run(go())
    assert tup.checkpoint["channel_values"] == {"messages": ["third"]}


def test_time_travel_to_a_named_checkpoint(graph):
    saver = CP.Neo4jSaver()

    async def go():
        wanted = None
        for text in ("first", "second"):
            cp = empty_checkpoint()
            cp["channel_values"] = {"messages": [text]}
            await saver.aput(_config(), cp, {}, {})
            if text == "first":
                wanted = cp["id"]
        return await saver.aget_tuple(_config(checkpoint_id=wanted))

    tup = asyncio.run(go())
    assert tup.checkpoint["channel_values"] == {"messages": ["first"]}


def test_pending_writes_come_back_with_the_tuple(graph):
    saver = CP.Neo4jSaver()
    checkpoint = empty_checkpoint()

    async def go():
        await saver.aput(_config(), checkpoint, {}, {})
        cfg = _config(checkpoint_id=checkpoint["id"])
        await saver.aput_writes(cfg, [("messages", "partial")], "task-1")
        return await saver.aget_tuple(_config())

    tup = asyncio.run(go())
    assert tup.pending_writes == [("task-1", "messages", "partial")]


def test_unknown_thread_reads_as_no_memory(graph):
    """A first turn has no checkpoint. It must read as empty, not raise —
    otherwise every new conversation 500s."""
    saver = CP.Neo4jSaver()
    assert asyncio.run(saver.aget_tuple(_config("never-seen"))) is None


def test_missing_thread_id_reads_as_no_memory(graph):
    saver = CP.Neo4jSaver()
    assert asyncio.run(saver.aget_tuple({"configurable": {}})) is None


# ── the bounds ──────────────────────────────────────────────────────────────

def test_put_without_a_thread_id_is_a_hard_error(graph):
    """Reading without a thread degrades to 'no memory'; WRITING without one
    would silently drop the conversation, so it raises instead."""
    saver = CP.Neo4jSaver()
    with pytest.raises(ValueError, match="thread_id"):
        asyncio.run(saver.aput({"configurable": {}}, empty_checkpoint(), {}, {}))


def test_oversized_checkpoint_is_refused(graph):
    """Storing it would make the NEXT resume load it back on a 512 MB box."""
    saver = CP.Neo4jSaver()
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": ["x" * (CP.MAX_CHECKPOINT_BYTES + 1)]}
    with pytest.raises(ValueError, match="over the"):
        asyncio.run(saver.aput(_config(), checkpoint, {}, {}))
    assert not graph.checkpoints, "an over-cap checkpoint must not be stored"


def test_write_prunes_in_the_same_query(graph):
    """A thread must not grow without bound between conversations. The prune
    rides on the write so there is no separate job to forget to run."""
    saver = CP.Neo4jSaver()
    asyncio.run(saver.aput(_config(), empty_checkpoint(), {}, {}))
    cypher = next(c for c in graph.seen if "MERGE (c:Checkpoint" in c)
    assert "SKIP $keep" in cypher and "DETACH DELETE old" in cypher


def test_delete_thread_forgets_that_thread_only(graph):
    saver = CP.Neo4jSaver()

    async def go():
        await saver.aput(_config("keep-me"), empty_checkpoint(), {}, {})
        await saver.aput(_config("drop-me"), empty_checkpoint(), {}, {})
        await saver.adelete_thread("drop-me")
        return (await saver.aget_tuple(_config("keep-me")),
                await saver.aget_tuple(_config("drop-me")))

    kept, dropped = asyncio.run(go())
    assert kept is not None
    assert dropped is None


def test_conversation_node_is_merged_not_matched(graph):
    """On /lab the first turn can precede the conversation row the browser
    saves after the answer lands. MATCH would drop the checkpoint silently."""
    saver = CP.Neo4jSaver()
    asyncio.run(saver.aput(_config(), empty_checkpoint(), {}, {}))
    cypher = next(c for c in graph.seen if "MERGE (c:Checkpoint" in c)
    assert "MERGE (conv:Conversation {id: $tid})" in cypher
    assert "ON CREATE SET" in cypher, "must not overwrite a real title"


# ── async-only ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,args", [
    ("get_tuple", ({},)),
    ("list", ({},)),
    ("put", ({}, empty_checkpoint(), {}, {})),
    ("put_writes", ({}, [], "task")),
])
def test_sync_methods_refuse(method, args):
    """A sync checkpointer call from the async request path would open a
    blocking driver session inside the event loop and stall every other
    in-flight request on a 0.5 vCPU box. It must say so, not do it."""
    saver = CP.Neo4jSaver()
    with pytest.raises(NotImplementedError, match="async-only"):
        getattr(saver, method)(*args)


def test_serialization_is_not_reimplemented():
    """The base class already ships JsonPlusSerializer, which handles
    LangChain messages and datetimes. Rolling our own is how a memory store
    starts silently corrupting history."""
    saver = CP.Neo4jSaver()
    assert type(saver.serde).__name__ == "JsonPlusSerializer"
