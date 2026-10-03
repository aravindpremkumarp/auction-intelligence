"""The one write of ``Document.extraction_json``, for every reader and every caller.

Three writers used to exist (the cron loader, the re-read script, the review
page's re-run), each with its own Cypher and its own idea of what a rewrite
owes the reviewer. ``write_extraction`` is now the only one. Before it
overwrites a stored read it:

1. keeps the read it is replacing (``extraction_prev_json`` with the reader
   and time that made it) — the one-level history scripts/revert_extraction
   restores from;
2. moves the reviewer's corrections onto the new entities
   (pipeline/extraction_ids.carry_corrections), orphaning what matches
   nothing rather than letting it land on the wrong entity;
3. with ``keep_better``, lets the stored read win when the new one is not
   better (pipeline/keep_better.best), raising :class:`KeptExisting`.

Then it stamps what the reader reported: the reader and prompt hash, the
schema version, the evidence counts the review queue filters on
(``extraction_contested`` / ``_fuzzy`` / ``_illegible`` / ``_dropped``), the
compact telemetry, the dated events (``:AuctionEvent``), the run
(``:ExtractionRun``), ``extraction_text_hash`` (so a re-read of the same text
is recognisable), and in shadow mode the v2 read beside the v1 one.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from api.neo4j_client import run_query, run_read_query
from pipeline.absence import AUTO, MUST_HAVE, clear_auto_marks, write_marks
from pipeline.extraction_ids import carry_corrections
from pipeline.keep_better import best
from pipeline.key_entities import absent_key, stamp_key_scores, unfound_key
from pipeline.validators import SCORE_VERSION, validate_stored

RULE_READER_NOT_STATED = "reader_not_stated"


class KeptExisting(Exception):
    """The stored read was at least as good; nothing was written."""


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
    """One row per target: its re-anchored corrections and its previous read."""
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


def _when(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def stored(filename: str) -> dict:
    """The stored extraction, and whether the notice's text changed since it
    was read (its spans then point into a text that is gone)."""
    rows = run_read_query(
        "MATCH (d:Document {filename: $fn}) "
        "RETURN d.extraction_json AS j, toString(d.extraction_at) AS xa, "
        "       toString(d.markdown_loaded_at) AS ma, toString(d.stitched_at) AS sa",
        {"fn": filename})
    r = rows[0] if rows else {}
    try:
        ents = json.loads(r["j"]) if r.get("j") else []
    except (TypeError, ValueError):
        ents = []
    read_at = _when(r.get("xa"))
    text_at = max((t for t in (_when(r.get("ma")), _when(r.get("sa"))) if t), default=None)
    return {"entities": ents, "text_changed": bool(read_at and text_at and text_at > read_at)}


def evidence_counts(ents: list[dict], dropped: int = 0) -> dict:
    c = {"contested": 0, "fuzzy": 0, "illegible": 0, "dropped": int(dropped or 0)}
    for e in ents:
        if not isinstance(e, dict):
            continue
        ev = (e.get("attrs") or {}).get("evidence")
        if ev == "CONTESTED":
            c["contested"] += 1
        elif ev == "FUZZY_GROUNDED":
            c["fuzzy"] += 1
        elif ev == "ILLEGIBLE":
            c["illegible"] += 1
    return c


def not_stated_marks(marks: list) -> dict[str, dict]:
    """Correction entries for the key facts the reader says the notice does
    not state: an absent mark, or an unfound one for a fact every notice
    states (reserve price, auction date, the description)."""
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = {}
    for lot, key in marks or []:
        k = unfound_key(str(lot), key) if key in MUST_HAVE else absent_key(str(lot), key)
        out[k] = {"by": AUTO, "rule": RULE_READER_NOT_STATED, "at": at}
    return out


_WRITE = """
UNWIND $rows AS r
MATCH (d:Document {filename: r.fn})
SET d.extraction_prev_json = r.prev_j,
    d.extraction_prev_reader = r.prev_reader,
    d.extraction_prev_at = r.prev_at,
    d.extraction_corrections_json = r.cj,
    d.extraction_json = $j,
    d.extraction_reader = $reader,
    d.extraction_prompt_hash = $prompt_hash,
    d.extraction_schema_version = $schema_version,
    d.extraction_text_hash = $text_hash,
    d.extraction_score = $score,
    d.extraction_score_version = $score_version,
    d.extraction_at    = datetime(),
    d.extraction_batch = $batch,
    d.extraction_model = $model,
    d.extraction_contested = $counts.contested,
    d.extraction_fuzzy = $counts.fuzzy,
    d.extraction_illegible = $counts.illegible,
    d.extraction_dropped = $counts.dropped,
    d.extraction_telemetry_json = $telemetry,
    d.extraction_timeline_json = $timeline,
    d.extraction_segmentation = $segmentation,
    d.extraction_reused_from = CASE WHEN r.fn = $fn THEN NULL ELSE $fn END,
    // A verification is a statement about entities a person actually read.
    // These entities are new, so the old verdict cannot cover them.
    d.extraction_review_status = 'pending',
    // Fresh entities reflect the current markdown: the staleness marker
    // must not outlive the condition it describes.
    d.extraction_stale_at = NULL,
    d.extraction_skipped_reason = NULL
REMOVE d.extraction_verified_by, d.extraction_verified_at
RETURN d.filename
"""

_SHADOW = """
UNWIND $fns AS fn
MATCH (d:Document {filename: fn})
SET d.extraction_shadow_json = $j, d.extraction_shadow_score = $score,
    d.extraction_shadow_reader = 'v2', d.extraction_shadow_at = datetime(),
    d.extraction_shadow_model = $model, d.extraction_shadow_judge = $judge,
    d.extraction_shadow_telemetry_json = $telemetry, d.extraction_shadow_error = $error,
    d.extraction_shadow_seconds = $seconds
RETURN d.filename
"""

_RUN = """
MERGE (run:ExtractionRun {run_id: $run_id})
ON CREATE SET run.batch = $batch, run.reader = $reader, run.prompt_hash = $prompt_hash,
              run.schema_version = $schema_version, run.model = $model, run.at = datetime()
WITH run
UNWIND $fns AS fn
MATCH (d:Document {filename: fn})
MERGE (d)-[:EXTRACTED_BY]->(run)
RETURN count(*)
"""

_EVENTS = """
UNWIND $fns AS fn
MATCH (d:Document {filename: fn})
OPTIONAL MATCH (d)-[:HAS_EVENT]->(old:AuctionEvent)
DETACH DELETE old
WITH DISTINCT d
UNWIND $events AS ev
MERGE (ae:AuctionEvent {filename: d.filename, event: ev.event, lot_index: coalesce(ev.lot_index, '')})
SET ae.date = ev.date, ae.quote = ev.quote, ae.evidence = ev.evidence, ae.source = ev.source,
    ae.page = ev.page
MERGE (d)-[:HAS_EVENT]->(ae)
RETURN count(*)
"""


def write_extraction(d: dict, ents: list[dict], batch: int, *, reader: str = "langextract",
                     model: str | None = None, keep_better: bool = False,
                     keep_auto_marks: bool = False, meta: dict | None = None) -> dict:
    """Store ``ents`` as ``d``'s extraction (and its twins'). Returns a
    summary; raises :class:`KeptExisting` when ``keep_better`` and the stored
    read is at least as good.

    A fresh read may number its lots differently, so it drops the automatic
    "not in the notice" marks made against the old one; ``keep_auto_marks``
    is for a save that only added facts to the stored read."""
    meta = meta or {}
    fn = d["filename"]
    md = d.get("md") or ""
    targets = d.get("twins") or [fn]
    how = "new"
    if keep_better:
        st = stored(fn)
        if st["entities"] and not st["text_changed"]:
            chosen, how, gains, losses = best(st["entities"], ents, md, d.get("expected_lot_count"))
            if chosen is None:
                why = ("lost " + ", ".join(losses[:5])) if losses else "no gain"
                raise KeptExisting(f"not better ({why}) — keeping the existing one")
            ents = chosen
            label = "better" if how == "new" else "merged with the stored read"
            print(f"    {fn}: {label} — {', '.join(gains[:6])}" + (" …" if len(gains) > 6 else ""),
                  flush=True)
    ents, reordered = in_notice_order(fn, ents, md, d.get("expected_lot_count"))
    if reordered:
        print(f"    {fn}: lots renumbered to the notice's order ({reordered})", flush=True)
    score = validate_stored(ents, source_text=md)["score"]
    rows, carried = carry_rows(targets, ents)
    if carried.get("orphaned"):
        print(f"    {fn}: {carried['orphaned']} reviewer correction(s) orphaned by this "
              f"re-read — listed on the review page", flush=True)
    tel = meta.get("telemetry")
    counts = evidence_counts(ents, len(meta.get("dropped") or []) if isinstance(meta.get("dropped"), list)
                             else meta.get("dropped") or 0)
    run_query(_WRITE, {
        "fn": fn, "rows": rows, "j": json.dumps(ents, ensure_ascii=False),
        "reader": reader, "prompt_hash": meta.get("prompt_hash"),
        "schema_version": meta.get("schema_version"),
        "text_hash": hashlib.sha256(md.encode("utf-8")).hexdigest() if md else None,
        "score": score, "score_version": SCORE_VERSION, "batch": batch, "model": model,
        "counts": counts,
        "telemetry": json.dumps(_compact(tel), ensure_ascii=False) if tel else None,
        "timeline": json.dumps(meta.get("timeline"), ensure_ascii=False) if meta.get("timeline") else None,
        "segmentation": (meta.get("segmentation") or {}).get("strategy"),
    })
    run_query(_RUN, {"run_id": f"B{batch}-{reader}-{meta.get('prompt_hash') or 'v1'}",
                     "batch": batch, "reader": reader, "prompt_hash": meta.get("prompt_hash"),
                     "schema_version": meta.get("schema_version"), "model": model, "fns": targets})
    if meta.get("timeline"):
        run_query(_EVENTS, {"fns": targets, "events": meta["timeline"]})
    sh = meta.get("shadow")
    if sh:
        run_query(_SHADOW, {
            "fns": targets, "j": json.dumps(sh.get("entities"), ensure_ascii=False) if sh.get("entities") else None,
            "score": sh.get("score"), "model": sh.get("model"),
            "judge": json.dumps(sh.get("judge"), ensure_ascii=False) if sh.get("judge") else None,
            "telemetry": json.dumps(_compact(sh.get("telemetry")), ensure_ascii=False) if sh.get("telemetry") else None,
            "error": sh.get("error"), "seconds": sh.get("seconds")})
    if not keep_auto_marks:
        clear_auto_marks(targets)
    marks = not_stated_marks(meta.get("marks") or [])
    if marks:
        for t in targets:
            write_marks(t, marks)
    stamp_key_scores(targets)
    return {"filename": fn, "targets": len(targets), "score": score, "how": how,
            "entities": len(ents), "carried": carried, "counts": counts}


_LOT_MATCH_DECIDED = """
MATCH (r:ResolutionDecision {kind: 'lot-match'})
WHERE r.decided_by IS NOT NULL AND NOT r.decided_by STARTS WITH 'system:'
  AND r.payload_json CONTAINS $key
RETURN count(r) AS n
"""


def in_notice_order(fn: str, ents: list[dict], md: str,
                    expected: int | None = None) -> tuple[list[dict], str | None]:
    """``ents`` with lots numbered in the notice's own order
    (pipeline/lot_order), and how it was decided — or ``ents`` unchanged and
    None when the order already matches, ``plan`` refuses, or a person has
    matched a listing to one of this notice's lots (that decision names a lot
    by number, so it is never moved silently)."""
    from pipeline.extraction_ids import assign_ids
    from pipeline.lot_order import apply as renumber, plan
    try:
        mapping, why = plan(ents, md, expected)
    except Exception:  # noqa: BLE001 - ordering is a nicety, never a failed save
        return ents, None
    if not mapping:
        return ents, None
    try:
        rows = run_read_query(_LOT_MATCH_DECIDED, {"key": json.dumps(fn)[1:-1] + "#"}, timeout=30.0)
        if rows and rows[0].get("n"):
            return ents, None
    except Exception:  # noqa: BLE001 - unsure who decided what: leave the numbers
        return ents, None
    moved, _ = renumber(ents, {}, mapping)
    return assign_ids(moved), why


def _compact(t: dict | None) -> dict | None:
    if not t:
        return None
    try:
        from pipeline.reader.telemetry import compact
        return compact(t)
    except Exception:  # noqa: BLE001 - a v1 telemetry dict has no compact form
        return t


__all__ = ["write_extraction", "KeptExisting", "in_notice_order", "previous", "carry_rows", "stored",
           "evidence_counts", "not_stated_marks", "RULE_READER_NOT_STATED"]
