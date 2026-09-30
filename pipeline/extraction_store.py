"""What every writer of ``Document.extraction_json`` does the same way.

Two writers exist today (pipeline/load_extractions for the cron path,
scripts/reset_langextract_and_extract for re-reads and the review UI's
re-run). Before either overwrites a stored read it must:

1. keep the read it is replacing (``extraction_prev_json`` and the reader
   and time that made it) — the one-level history that lets
   scripts/revert_extraction put a document back;
2. move the reviewer's corrections onto the new entities
   (pipeline/extraction_ids.carry_corrections), orphaning what matches
   nothing rather than letting it land on the wrong entity.

``carry_rows`` produces one row per target document with exactly those
values, for an ``UNWIND $rows`` write. PR6 moves the write itself here.
"""
from __future__ import annotations

from api.neo4j_client import run_read_query
from pipeline.extraction_ids import carry_corrections


def previous(filenames: list[str]) -> dict[str, dict]:
    """{filename: {j, cj, reader, at}} for the stored reads of ``filenames``.
    Missing documents, and a graph that cannot be read, give no entry."""
    if not filenames:
        return {}
    try:
        rows = run_read_query(
            "UNWIND $fns AS fn MATCH (d:Document {filename: fn}) "
            "RETURN fn, d.extraction_json AS j, d.extraction_corrections_json AS cj, "
            "       d.extraction_reader AS reader, toString(d.extraction_at) AS at",
            {"fns": list(filenames)})
    except Exception:  # noqa: BLE001 - a missing history is not a failed write
        return {}
    return {r["fn"]: {"j": r.get("j"), "cj": r.get("cj"), "reader": r.get("reader"),
                      "at": r.get("at")} for r in (rows or [])}


def carry_rows(targets: list[str], ents: list[dict]) -> tuple[list[dict], dict]:
    """One row per target: its re-anchored corrections and its previous read.
    ``report`` sums what moved and what was orphaned across the targets."""
    prev = previous(targets)
    rows, total = [], {}
    for fn in targets:
        p = prev.get(fn) or {}
        cj, rep = carry_corrections(p.get("j"), p.get("cj"), ents)
        for k, v in rep.items():
            total[k] = total.get(k, 0) + v
        rows.append({"fn": fn, "cj": cj, "prev_j": p.get("j"),
                     "prev_reader": p.get("reader") or ("langextract" if p.get("j") else None),
                     "prev_at": p.get("at")})
    return rows, total


__all__ = ["previous", "carry_rows"]
