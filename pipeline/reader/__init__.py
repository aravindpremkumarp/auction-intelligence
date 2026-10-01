"""Reader v2: a lot-first, schema-locked, code-grounded read of a sale notice.

Why this exists
---------------
The first-generation reader (pipeline/langextract_examples) asks one model
call to read a whole notice and copy long verbatim spans. It is unstable from
run to run, composes text that is not on the page, and loses lots on long
notices; a repair layer (gap_fill, absence, lot_windows, reground, ...) grew
around it to patch the output afterwards. This package moves every one of
those guarantees INTO the read:

  segment.py     lots are cut by code (table rows, serials, prices); a model is
                 asked only for boundary anchor phrases, and only as a fallback
  schema.py      the model fills a fixed form (Pydantic) with verbatim quotes;
                 it may answer ``not_stated`` and is told never to infer
  llm.py         structured-output client: temperature 0, seed, max_tokens,
                 finish_reason checked, three tiers of schema enforcement
  ground.py      every quote is located by code inside its own lot's text;
                 what cannot be located is dropped, never stored
  tables.py      HTML tables become rows × columns with char ranges, so a
                 money value must sit in its own row and column
  normalize.py   money / date / area / possession parsed by code; mangled
                 digits become ILLEGIBLE, never a guessed number
  provenance.py  char offset -> page + block, from Document.blocks
  migrate.py     forward migrations of stored entities across schema versions

  prompt.py      the prompts, rendered from the schema and two examples
  convert.py     the form -> extraction_json, every quote located
  consistency.py cross-field rules (EMD vs reserve, dates, UDS, extent)
  stability.py   targeted re-read of uncertain fields + key-facts vote
  telemetry.py   field-level counts and cost per key fact

``read_notice`` below runs them in order and is what pipeline/extract_entry
(PR6) and the eval's ``--reader v2`` call.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from pipeline.reader.schema import SCHEMA_VERSION  # noqa: F401

READER_VERSION = "v2"
NOTICE_HEAD_CHARS = 6000
NOTICE_TAIL_CHARS = 8000
BOUNDARY_MAX_CHARS = 60_000


@dataclass
class ReadResult:
    entities: list[dict]
    timeline: list[dict] = field(default_factory=list)
    segmentation: dict = field(default_factory=dict)
    dropped: list[dict] = field(default_factory=list)
    telemetry: dict = field(default_factory=dict)
    model: str = ""
    reader_version: str = READER_VERSION
    prompt_hash: str = ""
    marks: list[tuple[str, str]] = field(default_factory=list)      # (lot, key) not_stated
    findings: list = field(default_factory=list)
    reports: dict = field(default_factory=dict)


class EmptyRead(RuntimeError):
    """The model answered with no lots for a chunk that holds money."""


def _default_call(model_id: str, usage: dict, counter: dict):
    from pipeline.reader.llm import call_structured

    from pipeline.reader.llm import call_cost

    def call(system: str, user: str, schema: type, *, model: str | None = None):
        m = model or model_id
        r = call_structured(m, system, user, schema)
        counter["calls"] = counter.get("calls", 0) + 1
        counter["cost"] = counter.get("cost", 0.0) + call_cost(m, r.usage)
        for k, v in r.usage.items():
            usage[k] = usage.get(k, 0) + v
        return r.parsed
    return call


def read_notice(md: str, *, expected_lot_count: int | None = None,
                roster: list[dict] | None = None, notice_type: str | None = None,
                blocks: list[dict] | None = None, model_id: str | None = None,
                stability: str = "verify", call=None, notice_date: str | None = None
                ) -> ReadResult:
    """One notice, read the v2 way. ``call(system, user, schema, model=None)``
    is injectable for tests; by default pipeline/reader/llm against the
    routed model. Raises EmptyRead when a money-bearing chunk yields no lots
    on both the routed and the retry model — never returns an empty read."""
    from pipeline.extract_routing import select_extract_model, select_retry_model
    from pipeline.lot_chunks import _MONEY
    from pipeline.reader import consistency, stability as ST, telemetry
    from pipeline.reader.convert import to_entities
    from pipeline.reader.llm import Truncated
    from pipeline.reader.prompt import (PROMPT_HASH, system_prompt, user_boundaries,
                                        user_notice, user_segment)
    from pipeline.reader.schema import LotBoundaries, NoticeRead, SegmentRead
    from pipeline.reader.segment import chunks, describe, halve, segment
    from pipeline.reader.tables import parse_tables
    from pipeline.extraction_ids import assign_ids

    t0 = time.monotonic()
    usage: dict = {}
    counter: dict = {}
    if model_id is None:
        model_id, _ = select_extract_model(notice_type)
    retry_model, _ = select_retry_model()
    if call is None:
        call = _default_call(model_id, usage, counter)

    tables = parse_tables(md)

    def boundary_reader(text: str):
        return call(system_prompt("boundaries"),
                    user_boundaries(text[:BOUNDARY_MAX_CHARS], expected_lot_count), LotBoundaries)

    seg = segment(md, expected_lot_count, boundary_reader=boundary_reader, tables=tables)
    todo = chunks(seg, md)
    lot_reads = []
    chunk_of_lot: dict[int, object] = {}
    while todo:
        ch = todo.pop(0)
        k = None if seg.whole else len(ch.segment_ids)
        user = user_segment(ch.text, in_chunk=k, expected=expected_lot_count if seg.whole else None,
                            roster=roster)
        try:
            parsed = call(system_prompt("segment"), user, SegmentRead)
        except Truncated:
            halves = halve(ch, seg, md)
            if len(halves) == 2:
                todo = halves + todo
                continue
            raise
        lots = list(parsed.lots)
        if not lots and _MONEY.search(ch.text):
            parsed = call(system_prompt("segment"), user, SegmentRead, model=retry_model)
            lots = list(parsed.lots)
            if not lots:
                raise EmptyRead(f"no lots read from a money-bearing chunk {ch.segment_ids}")
        if seg.whole:
            for lot in lots:
                lot_reads.append((seg.segments[0], lot))
                chunk_of_lot[len(lot_reads) - 1] = ch
        else:
            # one LotRead per segment in the chunk, in order; extras are ignored,
            # a short answer leaves the trailing segments empty (re-read below)
            for j, sid in enumerate(ch.segment_ids):
                lot = lots[j] if j < len(lots) else None
                if lot is not None:
                    lot_reads.append((seg.segments[sid], lot))
                    chunk_of_lot[len(lot_reads) - 1] = ch
    lot_reads.sort(key=lambda sl: sl[0].start)

    head_tail = md[:NOTICE_HEAD_CHARS] + ("\n\n[...]\n\n" + md[-NOTICE_TAIL_CHARS:]
                                         if len(md) > NOTICE_HEAD_CHARS + NOTICE_TAIL_CHARS else "")
    notice = call(system_prompt("notice"), user_notice(head_tail), NoticeRead)

    conv = to_entities(notice, lot_reads, md, blocks=blocks, tables=tables,
                       prompt_hash=PROMPT_HASH, model=model_id,
                       head_end=seg.head_end or None,
                       tail_start=seg.tail_start if not seg.whole else None)
    entities = conv.entities
    seg_spans = {str(i + 1): (s.start, s.end) for i, (s, _) in enumerate(lot_reads)} if not seg.whole else {}
    findings = consistency.check(entities, notice_date=notice_date, segments=seg_spans)

    reports: dict = {"reread": [], "verify": []}
    if stability in ("verify", "double"):
        for lot, fields in ST.uncertain(entities, seg, md, conv.not_stated).items():
            i = int(lot) - 1
            ch = chunk_of_lot.get(i)
            if ch is None:
                continue
            entities, rep = ST.reread(entities, lot, fields, seg, ch, md, call,
                                      blocks=blocks, tables=tables, prompt_hash=PROMPT_HASH)
            reports["reread"].append(rep)
        seen = set()
        for i, ch in chunk_of_lot.items():
            if id(ch) in seen:
                continue
            seen.add(id(ch))
            reports["verify"].append(ST.verify(entities, seg, ch, md, call,
                                               max_reads=3 if stability == "double" else 2))
    entities = assign_ids(entities)
    tel = telemetry.summarise(entities, conv.dropped, usage=usage, calls=counter.get("calls", 0),
                              cost_usd=round(counter["cost"], 6) if "cost" in counter else None,
                              seconds=round(time.monotonic() - t0, 1),
                              segmentation=describe(seg), not_stated=conv.not_stated)
    return ReadResult(entities=entities, timeline=conv.timeline, segmentation=describe(seg),
                      dropped=conv.dropped, telemetry=tel, model=model_id,
                      prompt_hash=PROMPT_HASH, marks=conv.not_stated, findings=findings,
                      reports=reports)

