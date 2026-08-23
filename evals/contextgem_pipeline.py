"""ContextGem two-stage extraction prototype (A/B candidate vs LangExtract).

Why this exists
---------------
Production extraction (pipeline/langextract_examples.extract) is ONE call over the
whole notice: a ~5.5k-token prompt with seven few-shot examples, and the model must
find every lot AND bind every field to the right lot in a single pass. The known
failure mode lives in pipeline/extract_routing.char_buffer_for — LangExtract splits
the markdown into independent windows, so a long multi-lot notice loses its global
lot numbering; production works around it by inflating the window to 30k chars so
the whole notice stays in one call.

ContextGem models the same job as a *workflow* instead, which removes the workaround:

  Stage 1 (aspect)   — segment the notice into lots. One Aspect whose extracted
                       items ARE the per-lot description blocks. Cheap model.
  Stage 2 (concepts) — for each lot segment, extract that lot's fields from a
                       Document containing ONLY that lot's text. Lots cannot bleed
                       into each other because no lot's call ever sees another lot.
  Stage 3 (notice)   — notice-wide facts (legal basis, bank, borrower) from the
                       full text, where they belong.

Stages 2 and 3 run concurrently; stage 2 is one call per lot.

Output shape
------------
``extract_records()`` returns the SAME record dicts LangExtract is normalised into
by evals.langextract_eval._records — ``{"cls", "text", "attrs"}`` — so the existing
scorer (score_records / group_by_lot / score_multi) grades both engines identically.
This module deliberately owns no scoring logic.

Field semantics (identifier kind enum, the disjunctive-possession rule, money
normalisation) are ported from the canonical scheme in
pipeline/prompts/extract_enrichment.txt and the LangExtract guide, so the two
engines are asked for the same thing and the comparison measures the WORKFLOW,
not two different specs.

Run:  python -m evals.contextgem_pipeline evals/fixtures/750348.txt
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Literal

try:
    import contextgem as cg
except ModuleNotFoundError as e:  # pragma: no cover - environment guard
    if e.name != "contextgem":
        raise
    raise ModuleNotFoundError(
        "contextgem is not installed. It is a prototype-only dependency:\n"
        "    pip install contextgem"
    ) from e

from pipeline.config import (
    OPENROUTER_MODEL_EXTRACT_MULTI,
    OPENROUTER_MODEL_EXTRACT_SINGLE,
)

_KINDS_PATH = Path(__file__).resolve().parents[1] / "pipeline" / "lookups" / "identifier_kinds.json"
CANONICAL_KINDS: list[str] = json.loads(_KINDS_PATH.read_text(encoding="utf-8"))["canonical"]

# ── the rules, quoted from the canonical scheme so both engines get the same spec ──
_KIND_RULE = (
    "kind MUST be exactly one of: " + "|".join(CANONICAL_KINDS) + ". "
    "NEVER invent a kind and NEVER copy the document's own label as the kind. "
    "Map labels: 'T.S No'/'Sy No'/'S.F No'/'Survey No'/'R.S No' -> survey_old "
    "(survey_new when marked new/re-survey); 'Re Sy No'/'New S.No' -> survey_new; "
    "'Block No.18' -> kind=block value=18; 'CERSAI Security Interest Id' -> cersai."
)
_POSSESSION_RULE = (
    "Give a possession_type ONLY when the notice commits to ONE value for this lot. "
    "The boilerplate disjunction 'Constructive / Symbolic / Physical Possession' "
    "(preamble, or a 'Type of Possession' column, including the unfilled template "
    "'(mention whichever is applicable)') names all types without choosing — for that, "
    "return null: not the raw disjunction, not a guess."
)
_MONEY_RULE = (
    "Return integer rupees: strip 'Rs.', commas and '/-'. Indian digit grouping and OCR "
    "noise are common ('35.15,000/-' is 3515000). APPLY THE UNIT when the notice states "
    "one, in the value itself or in a column header: x100000 for Lakh, x10000000 for "
    "Crore — 'Rs.70.00 Lakhs' is 7000000, 'Rs. 45 lakh' is 4500000, "
    "'Reserve Price (In Lakhs) 572.34' is 57234000."
)

LOT_ASPECT_DESCRIPTION = """\
Each individual property lot offered for sale in this auction notice — one extracted
item per lot, in the order they appear.

A lot is one auctionable unit: its own property description block together with the
reserve price / EMD that belongs to it. A multi-lot notice lists several such blocks,
often numbered (Lot 1, Item A, SI.No.2) but frequently NOT numbered at all — in that
case each new "DESCRIPTION OF PROPERTY" / "All that piece and parcel..." block that is
followed by its own RESERVE PRICE starts a new lot.

Include for each lot: the whole property description (property type, every survey /
plot / patta / flat / door / CERSAI number, village, taluk, district, registration
district and sub-district, extent, all four boundaries) AND that lot's reserve price,
EMD and bid increment line.

EXCLUDE text shared by the whole notice: the preamble, the bank's name and authorised
officer, the terms and conditions, contact details, and the EMD remittance account.

Two flats in the same building, or two survey numbers sold under separate reserve
prices, are SEPARATE lots. A single property described across several paragraphs with
ONE reserve price is ONE lot."""

LOT_DETAIL_STRUCTURE = {
    "reserve_price_num": float | None,
    "emd_num": float | None,
    "property_type": str | None,
    "possession_type": Literal["symbolic", "physical", "constructive"] | None,
    "village": str | None,
    "taluk": str | None,
    "district": str | None,
    "registration_district": str | None,
    "registration_sub_district": str | None,
    "identifiers": [{"kind": Literal[tuple(CANONICAL_KINDS)], "value": str}],
}

LOT_DETAIL_DESCRIPTION = f"""\
The auction facts of the ONE property lot described in this text.

- reserve_price_num / emd_num: this lot's reserve price and EMD/deposit. {_MONEY_RULE}
- property_type: a short normalised phrase as written (residential flat, vacant land,
  house, commercial building, industrial shed...).
- possession_type: {_POSSESSION_RULE}
- village / taluk / district: the revenue location of the property. registration_district
  and registration_sub_district come from the "Registration District of X and Sub
  Registration District of Y" sentence — they are NOT the same as village/taluk/district.
- identifiers: EVERY id number stated for this lot, one entry each. {_KIND_RULE}
  Copy the value verbatim (keep '196/5', 'G-2', slashes and letters).

Use null for anything this text does not state. Do NOT infer from other lots — this
text is the only source."""


def _llm(model: str, role: str) -> cg.DocumentLLM:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    llm = cg.DocumentLLM(
        model=f"openrouter/{model}", api_key=key, role=role, temperature=0.0,
        max_tokens=int(os.environ.get("CONTEXTGEM_MAX_TOKENS", "8192")),
    )
    # Default rate is 3 calls / 10s, which serialises a 6-lot notice into a
    # minute of waiting and makes the latency number meaningless. The lot calls
    # are small and independent, so raise it.
    from aiolimiter import AsyncLimiter
    llm.async_limiter = AsyncLimiter(
        int(os.environ.get("CONTEXTGEM_MAX_CONCURRENCY", "8")), 1.0)
    return llm


def build_llm_group(segmenter_model: str | None = None,
                    detail_model: str | None = None) -> cg.DocumentLLMGroup:
    """The two-model group: a cheap segmenter and the production extraction model.

    Stage 1 only has to find where each lot starts and ends — a cheap model does that.
    Stage 2 reads one lot's text and must get its numbers exactly right, so it uses the
    same model production routes multi-lot notices to (OPENROUTER_MODEL_EXTRACT_MULTI),
    keeping the A/B honest: same model on the work that decides accuracy.
    """
    return cg.DocumentLLMGroup(llms=[
        _llm(segmenter_model or OPENROUTER_MODEL_EXTRACT_SINGLE, "extractor_text"),
        _llm(detail_model or OPENROUTER_MODEL_EXTRACT_MULTI, "reasoner_text"),
    ])


def _lot_document(text: str) -> cg.Document:
    doc = cg.Document(raw_text=text)
    doc.concepts = [cg.JsonObjectConcept(
        name="Lot details", description=LOT_DETAIL_DESCRIPTION,
        structure=LOT_DETAIL_STRUCTURE, singular_occurrence=True,
        llm_role="reasoner_text", add_justifications=True, justification_depth="brief",
    )]
    return doc


def _notice_document(markdown: str) -> cg.Document:
    doc = cg.Document(raw_text=markdown)
    doc.concepts = [
        cg.LabelConcept(
            name="Legal basis", labels=["SARFAESI", "DRT", "IBC"],
            classification_type="multi_class", singular_occurrence=True,
            llm_role="reasoner_text",
            description="The law the sale is held under. SARFAESI when the notice cites "
                        "the SARFAESI Act / Security Interest (Enforcement) Rules or an "
                        "Authorised Officer; DRT when a Recovery Officer / Debts Recovery "
                        "Tribunal conducts it; IBC for a liquidator sale.",
        ),
        cg.StringConcept(
            name="Secured creditor name", singular_occurrence=True,
            llm_role="reasoner_text",
            description="The bank or financial institution selling the property, as named "
                        "in the notice (e.g. 'Canara Bank'). For an ARC sale name the ARC. "
                        "Not the borrower, not the auction platform.",
        ),
        cg.StringConcept(
            name="Primary borrower", singular_occurrence=True,
            llm_role="reasoner_text",
            description="The first-named borrower whose dues are being recovered, as "
                        "written (include 'M/s.' and the proprietor clause if present). "
                        "Not a guarantor, not the bank.",
        ),
    ]
    return doc


def _segmenting_document(markdown: str) -> cg.Document:
    doc = cg.Document(raw_text=markdown)
    doc.aspects = [cg.Aspect(name="Auction lot", description=LOT_ASPECT_DESCRIPTION,
                             llm_role="extractor_text", reference_depth="paragraphs")]
    return doc


def _lot_texts(doc: cg.Document) -> list[str]:
    """The verbatim source text of each lot found in stage 1.

    Prefers the item's reference paragraphs (exact document text) over the item value,
    so stage 2 reads what the notice actually says rather than a model rendering of it.
    """
    out = []
    for item in doc.aspects[0].extracted_items:
        paras = [p.raw_text for p in (item.reference_paragraphs or [])]
        text = "\n\n".join(paras).strip() or str(item.value).strip()
        if text:
            out.append(text)
    return out


async def _extract_async(markdown: str, group: cg.DocumentLLMGroup) -> tuple[list[str], list[dict], dict]:
    seg = _segmenting_document(markdown)
    await group.extract_all_async(seg, use_concurrency=True)
    lot_texts = _lot_texts(seg)

    notice_doc = _notice_document(markdown)
    lot_docs = [_lot_document(t) for t in lot_texts]
    await asyncio.gather(*[
        group.extract_all_async(d, use_concurrency=True)
        for d in [notice_doc, *lot_docs]
    ])

    lots = []
    for d in lot_docs:
        items = d.concepts[0].extracted_items
        lots.append(dict(items[0].value) if items else {})

    notice = {}
    for concept in notice_doc.concepts:
        items = concept.extracted_items
        if not items:
            continue
        val = items[0].value
        notice[concept.name] = val[0] if isinstance(val, list) and val else val
    return lot_texts, lots, notice


def _fnum(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


_MERGE_KEYS = ("reserve_price_num", "emd_num", "property_type", "possession_type",
               "village", "taluk", "district", "registration_district",
               "registration_sub_district")


def assemble_lots(segments: list[dict]) -> list[dict]:
    """Fold stage-1 segments into auction lots, one lot per reserve price.

    Stage 1 segments on paragraph boundaries, so a lot usually arrives as TWO
    segments — the description block, then the "RESERVE PRICE ... EMD ..." line that
    sits in its own paragraph. Rather than ask the segmenter for cleverer boundaries
    (it cannot see the whole notice's numbering reliably — that is the problem we are
    avoiding), assemble deterministically from what stage 2 read:

        walk segments in document order, merging each into the lot being built;
        a segment carrying a reserve price CLOSES that lot.

    This encodes the same invariant the scorer uses — an auction lot is the unit that
    carries one reserve price (evals.langextract_eval.score_multi) — and uses the
    model's parsed numbers, not a regex over the raw text.

    Caveat: it assumes a lot's description precedes its reserve price. A notice that
    tabulates reserve prices ahead of the descriptions would mis-assemble; those show
    up as a lot-count mismatch rather than as silently mixed fields.
    """
    out: list[dict] = []
    cur: dict = {}
    for seg in segments:
        for key in _MERGE_KEYS:
            if cur.get(key) in (None, "") and seg.get(key) not in (None, ""):
                cur[key] = seg[key]
        idents = cur.setdefault("identifiers", [])
        for ident in (seg.get("identifiers") or []):
            if isinstance(ident, dict) and ident.get("kind") and ident.get("value"):
                idents.append(ident)
        if _fnum(seg.get("reserve_price_num")) is not None:
            out.append(cur)
            cur = {}
    if any(v for k, v in cur.items()):
        out.append(cur)
    return out


def to_records(lots: list[dict], notice: dict) -> list[dict]:
    """Map the workflow's output onto the record shape the LangExtract scorer grades.

    ``cls``/``attrs`` names mirror evals.langextract_eval._records exactly, including
    the ``lot_index`` (1-based, as a string) that group_by_lot buckets on — here it is
    the position of the lot segment from stage 1, not a number the model invented.
    """
    recs: list[dict] = [{
        "cls": "secured_creditor", "text": notice.get("Secured creditor name") or "",
        "attrs": {"legal_basis": notice.get("Legal basis"),
                  "bank_name": notice.get("Secured creditor name")},
    }]
    if notice.get("Primary borrower"):
        recs.append({"cls": "borrower", "text": notice["Primary borrower"],
                     "attrs": {"lot_index": "1"}})

    for i, lot in enumerate(lots, start=1):
        li = str(i)
        prop = {"lot_index": li}
        if lot.get("property_type"):
            prop["property_type"] = lot["property_type"]
        if lot.get("possession_type"):
            prop["possession_type"] = lot["possession_type"]
        recs.append({"cls": "property", "text": "", "attrs": prop})

        terms = {"lot_index": li}
        for key in ("reserve_price_num", "emd_num"):
            num = _fnum(lot.get(key))
            if num is not None:
                terms[key] = num
        if len(terms) > 1:
            recs.append({"cls": "auction_terms", "text": "", "attrs": terms})

        loc = {k: lot[k] for k in ("village", "taluk", "district",
                                   "registration_district", "registration_sub_district")
               if lot.get(k)}
        if loc:
            recs.append({"cls": "location", "text": "", "attrs": {**loc, "lot_index": li}})

        for ident in (lot.get("identifiers") or []):
            if isinstance(ident, dict) and ident.get("kind") and ident.get("value"):
                recs.append({"cls": "identifier", "text": str(ident["value"]),
                             "attrs": {"kind": ident["kind"], "value": str(ident["value"]),
                                       "lot_index": li}})
    return recs


def extract_records(markdown: str, group: cg.DocumentLLMGroup | None = None) -> list[dict]:
    """Run the whole workflow over one notice and return scorer-ready records."""
    group = group or build_llm_group()
    _, segments, notice = asyncio.run(_extract_async(markdown, group))
    return to_records(assemble_lots(segments), notice)


def main(paths: list[str]) -> int:
    group = build_llm_group()
    for p in paths:
        md = Path(p).read_text(encoding="utf-8")
        texts, segments, notice = asyncio.run(_extract_async(md, group))
        lots = assemble_lots(segments)
        print(f"\n=== {p} — {len(texts)} segment(s) -> {len(lots)} lot(s) ===")
        print(f"notice: {notice}")
        for i, lot in enumerate(lots, start=1):
            print(f"  lot{i}: {lot}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1:]))
