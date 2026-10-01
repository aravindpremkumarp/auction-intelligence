"""The prompts the reader sends, built from the schema and two examples.

One system prompt per read kind:

* ``segment``    — a chunk of a few lots (schema.SegmentRead);
* ``notice``     — the notice's head and tail, once (schema.NoticeRead);
* ``boundaries`` — first/last words of each lot (schema.LotBoundaries), the
                   segmentation fallback;
* ``keyfacts``   — the short verify read (schema.KeyFactsRead);
* ``reread``     — a few named fields of one lot (a dynamic subset of LotRead).

Every prompt carries the RULES below and the catalogue rendered from the
schema, so a change to a field's description reaches the model without a
prompt file to keep in step. ``PROMPT_HASH`` is stamped on every write so a
stored read says which prompt produced it.

The portal roster is rendered the way the LangExtract prompt renders it (one
compact line per listing) but asks for ``portal_aid`` on the lot, not on an
entity.
"""
from __future__ import annotations

import hashlib
import json

from pipeline.reader.examples import EXAMPLES
from pipeline.reader.schema import SCHEMA_VERSION, render_catalogue

RULES: tuple[str, ...] = (
    "QUOTE, NEVER COMPOSE. Every quote is ONE contiguous run copied exactly from "
    "the text you were given. Never join pieces from different places, never "
    "paraphrase, never fix spelling. Code will look for your quote in the text; "
    "a quote it cannot find is thrown away.",
    "PREFER not_stated OVER INFERENCE. If the lot's text does not state a fact, "
    "answer status=not_stated. Never infer a district, taluk or state from a "
    "village or city name; never fill a field from general knowledge or from "
    "another lot.",
    "ILLEGIBLE IS A STATUS. When the text is there but garbled by OCR (a price "
    "like '35.15,000', a letter inside a number), answer status=illegible with "
    "the garbled quote. Do not repair it.",
    "NO ARITHMETIC. Give money as the figure quoted plus its unit (rupees, lakh, "
    "crore) — from the figure itself or from the column header. Give areas with "
    "their unit. Give dates as quoted plus your ISO reading; code checks it.",
    "ONE LOT, ONE SET OF FACTS. A lot's reserve price, EMD, borrower and "
    "description are the ones in ITS row or ITS block. Never take a value from "
    "the neighbouring lot. In a table, the header names the column; the value "
    "in that column of the lot's own row is the lot's.",
    "LONG BLOCKS ARE ANCHORS. For the property description and the terms, give "
    "the first 4–12 words and the last 4–12 words of the block, verbatim. Do not "
    "copy the block.",
    "REGISTRATION DISTRICTS are the separate registration hierarchy: 'within the "
    "Registration District of X and the Sub-Registration District of Y' (either "
    "order), 'X Registration District', 'S.R.O. Y' / 'SRO: Y'. Keep them OUT of "
    "district and taluk.",
    "'X Hobli' is hobli (Karnataka), not a taluk. 'X Grama Panchayath' is "
    "panchayat. 'within the limits of X Corporation' is municipality_corporation.",
    "DRT case refs ('OA No', 'RC No', 'RP No', 'TRC No') and IBC refs ('CP(IB)', "
    "'IA', an NCLT order) are the notice's court_reference — never a loan account.",
    "ARC notices: the ARC is the seller (bank_name_quote); the bank the debt was "
    "assigned FROM is assignor_bank; the '... Trust' is trust_name; legal_basis "
    "stays SARFAESI.",
    "SURVEY NUMBERS: 'R.S No', 'T.S No', 'S.F No', 'Sy No', 'Survey No' are "
    "survey_old; 'Re Sy No', 'resurvey no', 'New S.No' are survey_new. Keep the "
    "subdivision suffix (72/1B). 'Block No.18' is kind=block value=18.",
    "FLATS: property_type_norm=flat. The flat number, floor and block are "
    "identifiers (kinds flat, floor, block). A flat's own area is built_up_area; "
    "its undivided share of land is undivided_share; the whole plot the share "
    "is carved from is uds_parent_extent and NEVER total_area.",
    "BOUNDARIES: adjacency_quote is WHAT lies on a side ('Road', 'Plot of Mr X'); "
    "measurement_quote is the DIMENSION along it ('40 Feet'). Check all four "
    "sides every time; a notice that gives adjacencies usually gives dimensions "
    "later in the text.",
    "POSSESSION: answer possession_type only when the lot's text commits to ONE "
    "kind. The menu 'Constructive / Symbolic / Physical Possession' and the "
    "template '(mention whichever is applicable)' name all kinds without "
    "choosing: that is not_stated. A possession DATE is a date with "
    "event=possession_date.",
    "SHARED FACTS go in the notice read: an auction date, deadline or possession "
    "statement the notice makes ONCE for every lot belongs in shared, not "
    "repeated on each lot. A lot's own dates go on the lot.",
    "LOTS IN THIS CHUNK ONLY. The text may carry the notice head, a table "
    "header and the notice tail as context. Return lots[] for the lots whose "
    "own description is in the chunk, in the order they appear; nothing for the "
    "context.",
    "extras: at most 5, only decision-relevant facts no other field covers "
    "(RERA / GST numbers, leasehold tenure, road access, pending litigation, "
    "tax dues). Never restate terms-of-sale boilerplate.",
)


def rules_text() -> str:
    return "RULES:\n" + "\n".join(f"- {r}" for r in RULES)


def _example_block() -> str:
    parts = []
    for i, (text, seg, notice) in enumerate(EXAMPLES, 1):
        parts.append(f"=== EXAMPLE {i}: TEXT ===\n{text}\n\n"
                     f"=== EXAMPLE {i}: lots (SegmentRead) ===\n"
                     f"{json.dumps(seg, ensure_ascii=False)}\n\n"
                     f"=== EXAMPLE {i}: notice (NoticeRead) ===\n"
                     f"{json.dumps(notice, ensure_ascii=False)}")
    return "\n\n".join(parts)


_TASK = {
    "segment": (
        "You read ONE chunk of an Indian bank auction sale notice (SARFAESI / DRT "
        "/ IBC) and fill in the SegmentRead form: one LotRead per auction lot "
        "whose description is in the chunk. Answer in JSON matching the schema "
        "exactly."),
    "notice": (
        "You read the HEAD and TAIL of an Indian bank auction sale notice and fill "
        "in the NoticeRead form: who sells, under which law, contacts, the EMD "
        "account, where the terms block starts and ends, and the facts the "
        "notice states ONCE for every lot (shared). Answer in JSON matching the "
        "schema exactly."),
    "boundaries": (
        "You read an Indian bank auction sale notice that sells several lots and "
        "answer ONLY where each lot's text starts and ends: its first 4–12 words "
        "and its last 4–12 words, verbatim, in document order, one entry per lot. "
        "A lot is one property with its own reserve price. Answer in JSON matching "
        "the schema exactly."),
    "keyfacts": (
        "You check a chunk of an Indian bank auction sale notice and answer, for "
        "each lot in it, only the key facts: reserve price, EMD, auction start "
        "date, property type, possession, extent, village, taluk and the first "
        "words of the description. Quote verbatim; answer not_stated when the lot "
        "does not state it. Answer in JSON matching the schema exactly."),
    "reread": (
        "You re-read ONE lot of an Indian bank auction sale notice for a few named "
        "fields an earlier read left uncertain. Quote verbatim from the lot's own "
        "text; answer not_stated when it does not state the fact. Answer in JSON "
        "matching the schema exactly."),
}


def system_prompt(kind: str) -> str:
    """The system prompt for one read kind."""
    if kind not in _TASK:
        raise ValueError(f"unknown prompt kind {kind!r}")
    parts = [_TASK[kind], rules_text()]
    if kind in ("segment", "notice", "reread", "keyfacts"):
        parts.append(render_catalogue())
    if kind in ("segment", "notice"):
        parts.append(_example_block())
    return "\n\n".join(parts)


def _hash() -> str:
    h = hashlib.sha1()
    for k in sorted(_TASK):
        h.update(system_prompt(k).encode("utf-8"))
    return f"v2-s{SCHEMA_VERSION}-{h.hexdigest()[:10]}"


PROMPT_HASH = _hash()

# ── user prompts ──────────────────────────────────────────────────────────────
MAX_ROSTER_ROWS = 40
MAX_ROSTER_DESC_CHARS = 140


def _clip(value, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _roster_row(row: dict) -> str | None:
    parts: list[str] = []
    if row.get("reserve") is not None:
        parts.append(f"reserve {int(row['reserve'])}")
    if row.get("emd") is not None:
        parts.append(f"emd {int(row['emd'])}")
    where = " ".join(str(v).strip() for v in (row.get("village"), row.get("district")) if v)
    if where:
        parts.append(where)
    for key in ("area", "ptype"):
        if row.get(key):
            parts.append(str(row[key]).strip())
    if row.get("borrower"):
        parts.append(f"borrower {_clip(row['borrower'], 60)}")
    if row.get("desc"):
        parts.append(_clip(row["desc"], MAX_ROSTER_DESC_CHARS))
    if not parts:
        return None
    line = " | ".join(parts)
    return f"listing {row['aid']}: {line}" if row.get("aid") else line


def roster_block(roster: list[dict] | None) -> str:
    """The notice's portal listings as reference context, or ""."""
    lines = [ln for ln in (_roster_row(r) for r in (roster or [])) if ln]
    if not lines:
        return ""
    shown, hidden = lines[:MAX_ROSTER_ROWS], max(0, len(lines) - MAX_ROSTER_ROWS)
    body = "\n".join(f"  - {ln}" for ln in shown)
    if hidden:
        body += f"\n  - (+{hidden} further listings not shown)"
    return (
        "\n\n=== PORTAL LISTINGS FOR THIS NOTICE (reference only) ===\n"
        f"The auction portal separately lists {len(lines)} lot(s) for this notice. "
        "This is external data, NOT part of the notice. Use it only to judge where "
        "one lot ends and the next begins and to say WHICH listing each lot is "
        "(portal_aid = the id in the `listing <id>:` label). NEVER copy a value "
        "from this list: every value you give must be a quote from the notice. The "
        "rows are in no particular order. One listing belongs to at most one lot; "
        "omit portal_aid when unsure.\n\n" + body + "\n"
    )


def lot_count_hint(expected: int | None, in_chunk: int | None = None) -> str:
    if in_chunk is not None:
        return (f"\n\nThis chunk holds {in_chunk} lot(s), cut by their own numbering or "
                f"price lines. Return exactly {in_chunk} entries in lots[], in order.")
    if expected is None:
        return ""
    if int(expected) <= 1:
        return ("\n\nA reviewer confirmed this notice sells EXACTLY ONE lot: several "
                "schedules or items sharing one reserve price are parts of the same lot.")
    return (f"\n\nA reviewer confirmed this notice sells EXACTLY {int(expected)} lots. "
            f"Return all {int(expected)}, in document order; do not merge or invent.")


def user_segment(text: str, *, in_chunk: int | None = None, expected: int | None = None,
                 roster: list[dict] | None = None, hint: str | None = None) -> str:
    extra = f"\n\n{hint.strip()}" if hint and hint.strip() else ""
    return ("=== NOTICE CHUNK ===\n" + text + lot_count_hint(expected, in_chunk)
            + roster_block(roster) + extra)


def user_notice(head_tail: str) -> str:
    return "=== NOTICE HEAD AND TAIL ===\n" + head_tail


def user_boundaries(md: str, expected: int | None = None) -> str:
    return "=== NOTICE ===\n" + md + lot_count_hint(expected)


def user_keyfacts(text: str, in_chunk: int) -> str:
    return ("=== NOTICE CHUNK ===\n" + text +
            f"\n\nThis chunk holds {in_chunk} lot(s). Return exactly {in_chunk} entries "
            "in lots[], in order.")


def user_reread(text: str, fields: list[str]) -> str:
    return ("=== ONE LOT (with the notice head and tail as context) ===\n" + text +
            "\n\nFields needed: " + ", ".join(fields) + ". Answer only those.")


__all__ = ["RULES", "PROMPT_HASH", "system_prompt", "user_segment", "user_notice",
           "user_boundaries", "user_keyfacts", "user_reread", "roster_block",
           "lot_count_hint", "rules_text"]
