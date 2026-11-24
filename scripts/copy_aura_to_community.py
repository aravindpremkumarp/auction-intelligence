"""Copy the whole graph from Neo4j Aura into a self-hosted Neo4j Community.

Why a copy script instead of dump/load: Aura stores data in the Enterprise-only
"block" format, which Community cannot load. This streams every node and
relationship over Bolt instead, then recreates the constraints and indexes.

Steps (run all, or one at a time with --step):

  nodes    MERGE every node by its Aura elementId (temp label :_Migr + prop
           _src_id). Safe to re-run: existing nodes are updated, not duplicated.
  rels     Per relationship type: skipped if the target count already matches,
           otherwise that type is wiped in the target and recreated.
  verify   Compare per-label and per-type counts. Exit code 1 on mismatch.
  cleanup  Remove the temp label/property and the temp index.
  schema   Recreate Aura's constraints and indexes (skips ones that exist).

Environment:
  SRC_URI SRC_USER SRC_PASSWORD SRC_DATABASE   Aura (defaults: NEO4J_* vars)
  DST_URI DST_USER DST_PASSWORD DST_DATABASE   Community (DST_DATABASE=neo4j)

Run on the VM, next to the target:
  DST_URI=bolt+ssc://localhost:7687 python scripts/copy_aura_to_community.py
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict

from neo4j import GraphDatabase

MIGR_LABEL = "_Migr"
MIGR_PROP = "_src_id"
MIGR_INDEX = "_migr_src_id"
NODE_BATCH = 500   # small: some nodes carry 3072-float embeddings
REL_BATCH = 5000
STEPS = ("nodes", "rels", "verify", "cleanup", "schema")


def q(name: str) -> str:
    """Backtick-quote a label / type / name for Cypher."""
    return "`" + name.replace("`", "``") + "`"


def schema_statements(constraints: list[dict], indexes: list[dict]) -> list[tuple[str, str]]:
    """(name, createStatement) pairs to recreate, constraints first.

    LOOKUP indexes exist by default; indexes owned by a constraint are created
    by that constraint.
    """
    out = [(c["name"], c["createStatement"]) for c in constraints]
    out += [
        (i["name"], i["createStatement"])
        for i in indexes
        if i["type"] != "LOOKUP" and not i.get("owningConstraint")
    ]
    return out


def env(name: str, fallback: str | None = None, default: str | None = None) -> str:
    val = os.getenv(name) or (os.getenv(fallback) if fallback else None) or default
    if not val:
        sys.exit(f"Missing environment variable {name}")
    return val


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Copier:
    def __init__(self) -> None:
        self.src = GraphDatabase.driver(
            env("SRC_URI", "NEO4J_URI"),
            auth=(env("SRC_USER", "NEO4J_USERNAME"), env("SRC_PASSWORD", "NEO4J_PASSWORD")),
        )
        self.src_db = env("SRC_DATABASE", "NEO4J_DATABASE")
        self.dst = GraphDatabase.driver(
            env("DST_URI"),
            auth=(env("DST_USER", default="neo4j"), env("DST_PASSWORD")),
        )
        self.dst_db = env("DST_DATABASE", default="neo4j")
        self.src.verify_connectivity()
        self.dst.verify_connectivity()

    def close(self) -> None:
        self.src.close()
        self.dst.close()

    # ── helpers ──────────────────────────────────────────────────────────────
    def src_rows(self, cypher: str, **params):
        with self.src.session(database=self.src_db, fetch_size=1000) as s:
            yield from s.run(cypher, **params)

    def dst_run(self, cypher: str, **params) -> list:
        with self.dst.session(database=self.dst_db) as s:
            return list(s.run(cypher, **params))

    def dst_write(self, cypher: str, **params) -> None:
        with self.dst.session(database=self.dst_db) as s:
            s.execute_write(lambda tx: tx.run(cypher, **params).consume())

    def _counts(self, which: str, cypher: str) -> dict[str, int]:
        rows = list(self.src_rows(cypher)) if which == "src" else self.dst_run(cypher)
        return {r["k"]: r["c"] for r in rows if r["k"] != MIGR_LABEL}

    def labels(self, which: str) -> dict[str, int]:
        return self._counts(which, "MATCH (n) UNWIND labels(n) AS k RETURN k, count(*) AS c")

    def rel_types(self, which: str) -> dict[str, int]:
        return self._counts(which, "MATCH ()-[r]->() RETURN type(r) AS k, count(*) AS c")

    def total(self, which: str, cypher: str) -> int:
        rows = list(self.src_rows(cypher)) if which == "src" else self.dst_run(cypher)
        return rows[0]["c"]

    # ── steps ────────────────────────────────────────────────────────────────
    def step_nodes(self) -> None:
        dst_nodes = self.total("dst", "MATCH (n) RETURN count(n) AS c")
        migr = self.total("dst", f"MATCH (n:{MIGR_LABEL}) RETURN count(n) AS c")
        if dst_nodes and dst_nodes != migr:
            sys.exit(
                f"Target already holds {dst_nodes - migr:,} nodes that this copy did not create "
                "(or a finished copy was cleaned up). Re-running would duplicate them. "
                "Wipe the target first: MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS"
            )
        self.dst_write(
            f"CREATE INDEX {MIGR_INDEX} IF NOT EXISTS FOR (n:{MIGR_LABEL}) ON (n.{MIGR_PROP})"
        )
        self.dst_write("CALL db.awaitIndexes(300)")
        src_labels = self.labels("src")
        log(f"nodes: {sum(src_labels.values()):,} label memberships across {len(src_labels)} labels")
        done = 0
        # Every label, then nodes with no label at all (None).
        unlabelled = self.total("src", "MATCH (n) WHERE size(labels(n)) = 0 RETURN count(n) AS c")
        work = sorted(src_labels.items(), key=lambda kv: kv[1]) + ([(None, unlabelled)] if unlabelled else [])
        for label, total in work:
            match = f"MATCH (n:{q(label)})" if label else "MATCH (n) WHERE size(labels(n)) = 0"
            groups: dict[tuple, list] = defaultdict(list)
            sent = 0
            for r in self.src_rows(
                f"{match} RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props"
            ):
                key = tuple(sorted(r["labels"]))
                groups[key].append({"id": r["id"], "props": r["props"]})
                if len(groups[key]) >= NODE_BATCH:
                    self._write_nodes(key, groups.pop(key))
                sent += 1
            for key, rows in groups.items():
                self._write_nodes(key, rows)
            done += sent
            log(f"  {label or '(no label)'}: {sent:,}/{total:,}")
        log(f"nodes done ({done:,} rows streamed)")

    def _write_nodes(self, labels: tuple, rows: list) -> None:
        set_labels = f" SET n{''.join(f':{q(l)}' for l in labels)}" if labels else ""
        self.dst_write(
            f"UNWIND $rows AS r "
            f"MERGE (n:{MIGR_LABEL} {{{MIGR_PROP}: r.id}}) "
            f"SET n = r.props SET n.{MIGR_PROP} = r.id{set_labels}",
            rows=rows,
        )

    def step_rels(self) -> None:
        src_types = self.rel_types("src")
        dst_types = self.rel_types("dst")
        log(f"rels: {sum(src_types.values()):,} across {len(src_types)} types")
        for t, total in sorted(src_types.items(), key=lambda kv: kv[1]):
            if dst_types.get(t) == total:
                log(f"  {t}: already {total:,}, skipped")
                continue
            if dst_types.get(t):
                log(f"  {t}: partial ({dst_types[t]:,}/{total:,}), wiping and recopying")
                with self.dst.session(database=self.dst_db) as s:
                    s.run(
                        f"MATCH ()-[r:{q(t)}]->() CALL (r) {{ DELETE r }} IN TRANSACTIONS OF 10000 ROWS"
                    ).consume()
            batch: list = []
            sent = 0
            for r in self.src_rows(
                f"MATCH (a)-[r:{q(t)}]->(b) "
                f"RETURN elementId(a) AS s, elementId(b) AS t, properties(r) AS props"
            ):
                batch.append({"s": r["s"], "t": r["t"], "props": r["props"]})
                if len(batch) >= REL_BATCH:
                    self._write_rels(t, batch)
                    sent += len(batch)
                    batch = []
            if batch:
                self._write_rels(t, batch)
                sent += len(batch)
            log(f"  {t}: {sent:,}/{total:,}")
        log("rels done")

    def _write_rels(self, rel_type: str, rows: list) -> None:
        self.dst_write(
            f"UNWIND $rows AS r "
            f"MATCH (a:{MIGR_LABEL} {{{MIGR_PROP}: r.s}}) "
            f"MATCH (b:{MIGR_LABEL} {{{MIGR_PROP}: r.t}}) "
            f"CREATE (a)-[x:{q(rel_type)}]->(b) SET x = r.props",
            rows=rows,
        )

    def step_verify(self) -> bool:
        ok = True
        for kind, fn in (("label", self.labels), ("rel type", self.rel_types)):
            src, dst = fn("src"), fn("dst")
            for name in sorted(set(src) | set(dst)):
                if src.get(name, 0) != dst.get(name, 0):
                    ok = False
                    log(f"  MISMATCH {kind} {name}: aura={src.get(name, 0):,} community={dst.get(name, 0):,}")
        node_q = "MATCH (n) RETURN count(n) AS c"
        rel_q = "MATCH ()-[r]->() RETURN count(r) AS c"
        sn, sr = self.total("src", node_q), self.total("src", rel_q)
        dn, dr = self.total("dst", node_q), self.total("dst", rel_q)
        log(f"totals: aura nodes={sn:,} rels={sr:,} | community nodes={dn:,} rels={dr:,}")
        ok = ok and sn == dn and sr == dr
        log("VERIFY OK" if ok else "VERIFY FAILED")
        return ok

    def step_cleanup(self) -> None:
        with self.dst.session(database=self.dst_db) as s:
            s.run(
                f"MATCH (n:{MIGR_LABEL}) CALL (n) {{ REMOVE n:{MIGR_LABEL}, n.{MIGR_PROP} }} "
                f"IN TRANSACTIONS OF 10000 ROWS"
            ).consume()
        self.dst_write(f"DROP INDEX {MIGR_INDEX} IF EXISTS")
        log("cleanup done")

    def step_schema(self) -> None:
        constraints = [dict(r) for r in self.src_rows(
            "SHOW CONSTRAINTS YIELD name, createStatement RETURN name, createStatement"
        )]
        indexes = [dict(r) for r in self.src_rows(
            "SHOW INDEXES YIELD name, type, owningConstraint, createStatement "
            "RETURN name, type, owningConstraint, createStatement"
        )]
        existing = {r["name"] for r in self.dst_run("SHOW CONSTRAINTS YIELD name RETURN name")}
        existing |= {r["name"] for r in self.dst_run("SHOW INDEXES YIELD name RETURN name")}
        failed = []
        for name, stmt in schema_statements(constraints, indexes):
            if name in existing:
                continue
            try:
                self.dst_write(stmt)
                log(f"  created {name}")
            except Exception as e:  # report and keep going; summary below
                failed.append(name)
                log(f"  FAILED {name}: {e}")
        self.dst_write("CALL db.awaitIndexes(1800)")
        log("schema done" + (f" — {len(failed)} failed: {', '.join(failed)}" if failed else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--step", choices=("all",) + STEPS, default="all")
    args = ap.parse_args()

    c = Copier()
    try:
        steps = STEPS if args.step == "all" else (args.step,)
        for step in steps:
            log(f"── {step} ──")
            if step == "verify":
                if not c.step_verify():
                    log("Stopping: fix the mismatch (re-run --step nodes / rels), then continue.")
                    return 1
            else:
                getattr(c, f"step_{step}")()
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
