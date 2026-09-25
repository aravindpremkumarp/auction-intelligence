"""Decide "not in the notice" without a reviewer.

A key fact a notice never states cannot be found by any number of re-reads.
Without a mark, the gap-filler (scripts/fill_gaps) would pay to look for it on
every run, and the review queue would hold it as a miss forever. Marking it by
hand is the bottleneck this module removes. Three rules, per lot and key:

1. **No clue in the text** (``no_clue``). The lot's own text has no word that
   could carry the fact — no "possession", "symbolic" or "physical" for
   possession; no unit of area for extent. Nothing to read, so it is marked
   absent without a model call.
2. **Read twice, found nothing.** The text has clues, but a lean read and then
   a read on the stronger model both came back without the fact. Marked absent.
3. **A fact every notice states** (reserve price, auction date, the property
   description — ``MUST_HAVE``) is never marked absent automatically. A notice
   without one has almost always lost it between the image and the text (a
   dropped table cell, a truncated page). It is marked *unfound* instead: the
   cell stays missing and goes to a person, who fixes the text or marks it,
   but the gap-filler stops looking.

Every automatic mark carries ``by: "auto"`` and the ``rule`` that made it, so
the review page shows it as automatic and a reviewer can undo it. Undoing one
leaves an *unfound* mark in its place (api/review/extraction.set_key_absent),
so the next run does not simply mark it again.

A fresh full read replaces the lots the marks were made against, so it drops
every automatic mark (``clear_auto_marks``); a person's marks stay.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from pipeline.key_entities import AUTO, absent_key, key_marks, unfound_key

#: Facts a sale notice always states. Not found → probably lost from the text.
MUST_HAVE = frozenset({"reserve_price", "auction_date", "full_description"})

#: Words without which the lot's text cannot state the fact. Deliberately
#: broad: a false clue costs one cheap read, a missed one a wrong "absent".
CLUES = {
    "possession_type": re.compile(r"possess|symbolic|physical|constructive", re.I),
    "extent": re.compile(
        r"\bsq|square|\bsft\b|\bcents?\b|acre|hect|\bares?\b|guntha|\bgrounds?\b"
        r"|extent|\barea\b|admeasur|measuring|\bkanal|marla|bigha|\bsq\.?\s*(?:ft|m|yd)",
        re.I),
    "property_type": re.compile(
        r"land|plot|flat|apartment|house|building|villa|shop|office|site|"
        r"residential|commercial|industrial|factory|godown|warehouse|premises|"
        r"bungalow|tenement|structure|survey|\bs\.?\s*no", re.I),
    "location": re.compile(
        r"village|taluk|tehsil|district|nagar|street|road|town|city|panchayat|"
        r"ward|registration|situated|lying|located|\bat\b", re.I),
}

RULE_NO_CLUE = "no_clue"
RULE_NOT_FOUND = "not_found"


def no_clue(text: str, key: str) -> bool:
    """True when ``text`` has nothing that could state ``key``. Never for a
    must-have fact, nor for one without a clue list."""
    rx = CLUES.get(key)
    if key in MUST_HAVE or rx is None:
        return False
    return rx.search(re.sub(r"<[^>]+>", " ", text or "")) is None


def verdict(key: str) -> str:
    """The mark for a fact looked for and not found: ``absent`` or ``unfound``."""
    return "unfound" if key in MUST_HAVE else "absent"


def skip(corrections: dict) -> set[tuple[str, str]]:
    """(lot, key) the gap-filler must not look for: every mark, a person's or
    automatic, absent or unfound."""
    return set(key_marks(corrections))


def new_marks(found: dict[tuple[str, str], str]) -> dict[str, dict]:
    """Correction entries for ``{(lot, key): rule}``."""
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = {}
    for (lot, key), rule in found.items():
        kind = verdict(key) if rule == RULE_NOT_FOUND else "absent"
        k = unfound_key(lot, key) if kind == "unfound" else absent_key(lot, key)
        out[k] = {"by": AUTO, "rule": rule, "at": at}
    return out


def _is_auto(k: str, v) -> bool:
    return (isinstance(v, dict) and v.get("by") == AUTO
            and (k.startswith("absent:") or k.startswith("unfound:")))


def write_marks(filename: str, marks: dict[str, dict]) -> int:
    """Add automatic marks to the document's corrections, never over a
    person's mark. Leaves the review status alone — nobody reviewed anything.
    Returns marks written."""
    from api.neo4j_client import run_query, run_read_query
    from pipeline.key_entities import stamp_key_scores
    if not marks:
        return 0
    rows = run_read_query(
        "MATCH (d:Document {filename:$fn}) WHERE d.extraction_json IS NOT NULL "
        "RETURN coalesce(d.extraction_corrections_json,'{}') AS c", {"fn": filename})
    if not rows:
        return 0
    try:
        corr = json.loads(rows[0]["c"] or "{}")
    except json.JSONDecodeError:
        corr = {}
    if not isinstance(corr, dict):
        corr = {}
    taken = key_marks(corr)
    n = 0
    for k, v in marks.items():
        _, lot, key = k.split(":", 2)
        held = taken.get((lot, key))
        if held and held.get("by") != AUTO:
            continue
        corr[k] = v
        n += 1
    if not n:
        return 0
    run_query(
        "MATCH (d:Document {filename:$fn}) WHERE d.extraction_json IS NOT NULL "
        "SET d.extraction_corrections_json = $c",
        {"fn": filename, "c": json.dumps(corr, ensure_ascii=False)})
    stamp_key_scores([filename])
    return n


def drop_auto(corrections: dict) -> dict:
    """``corrections`` without the automatic marks."""
    return {k: v for k, v in corrections.items() if not _is_auto(k, v)}


def clear_auto_marks(filenames: list[str]) -> None:
    """Drop the automatic marks on these documents — their lots were just
    re-read from scratch, so the marks may name the wrong lot now."""
    from api.neo4j_client import run_query, run_read_query
    rows = run_read_query(
        "UNWIND $fns AS fn MATCH (d:Document {filename: fn}) "
        "WHERE d.extraction_corrections_json CONTAINS '\"auto\"' "
        "RETURN d.filename AS f, d.extraction_corrections_json AS c",
        {"fns": list(filenames)})
    out = []
    for r in rows:
        try:
            corr = json.loads(r["c"] or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(corr, dict):
            kept = drop_auto(corr)
            if len(kept) != len(corr):
                out.append({"f": r["f"], "c": json.dumps(kept, ensure_ascii=False)})
    if out:
        run_query("UNWIND $rows AS row MATCH (d:Document {filename: row.f}) "
                  "SET d.extraction_corrections_json = row.c", {"rows": out})
