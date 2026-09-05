"""LangExtract few-shot examples + prompt for SARFAESI auction-notice extraction.

SOURCE OF TRUTH (option C): the field catalogue is NOT redefined here — it is read
at import time from ``pipeline/prompts/extract_enrichment.txt`` (the canonical
scheme) and wrapped with LangExtract-specific conventions. Edit the scheme in that
one file and this prompt follows automatically; the examples below only have to
keep *demonstrating* the fields.

The seven ExampleData objects (single: 736547 / Bank of Baroda; multi: 738029 /
Equitas SFB; apartment: Canara Bank / flat + UDS; DRT: Indian Bank / DRT-III
Chennai, 750600; ARC: Omkara ARC / IndusInd Bank, 747290; Karnataka leasehold:
Karnataka Bank, 752691; Can Fin multi w/ disjunctive possession:
CANFN17791720254760) are annotated to FULL PARITY (option A) with the scheme,
across these grounded entity classes — chosen so LangExtract extracts spans
(its strength) rather than long attribute lists (its weakness):

  secured_creditor  borrower  contact  property  full_description  location
  identifier  extent  boundary  schedule  auction_terms  outstanding  emd_account
  full_terms  extras

For MULTI notices every entity of the Nth lot carries ``lot_index=N`` so lots can
be regrouped into one AuctionProperty each. The DRT/ARC/Karnataka/CanFin examples
exist because attrs documented only as prose rules were being dropped: prose
alone demonstrably underperforms, and on the Gemini model_id path langextract
builds its response schema FROM THE EXAMPLES, so an attr key demonstrated
nowhere used to be suppressed outright (that is why hobli never appeared).
tests/api/test_langextract_prompt_coverage.py enforces that every guide-declared
attr key is demonstrated in at least one example or exempted with justification.
Rare fields with no demonstration source (liquidator, predecessor_entity,
lat/long, carpet_area, sarfaesi_stage, ...) remain prose-only — extracted when
present now that schema constraints are off on both provider paths.

Run:  python -m pipeline.langextract_examples <path-to-markdown.txt>
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import langextract as lx
except ModuleNotFoundError as e:  # pragma: no cover - environment guard
    # langextract is a batch-pipeline dependency and lives in
    # config/requirements.txt, NOT the top-level requirements.txt that Render
    # installs: it pulls pandas, google-cloud-storage, absl-py and
    # ml-collections, and the API server never imports it. That split is
    # deliberate, but it means any box provisioned from requirements.txt (a
    # cloud runner, a fresh container) looks fine until the first extraction
    # and then dies on a bare "No module named 'langextract'". Say what to do
    # instead.
    if e.name != "langextract":
        raise
    raise ModuleNotFoundError(
        "langextract is not installed. It is a batch-pipeline dependency kept "
        "out of the production requirements.txt on purpose — install the "
        "pipeline set instead:\n"
        "    pip install -r config/requirements.txt\n"
        "(or just `pip install 'langextract>=1.5,<2' 'openai>=2.0,<3'`)"
    ) from e

# --------------------------------------------------------------------------- #
# C — prompt is derived from the canonical scheme file, not hand-maintained.
# --------------------------------------------------------------------------- #
_CANONICAL_SCHEME_PATH = (
    Path(__file__).resolve().parent / "prompts" / "extract_enrichment.txt"
)


def load_canonical_scheme() -> str:
    """Read the authoritative field scheme (extract_enrichment.txt)."""
    return _CANONICAL_SCHEME_PATH.read_text(encoding="utf-8")


# LangExtract-specific wrapper. The *fields* come from the canonical scheme; this
# only tells the model how to shape them into LangExtract entity classes.
_LANGEXTRACT_GUIDE = """\
You extract structured data from an Indian bank auction sale notice. Emit grounded
entities using EXACTLY these extraction classes (one span each, copied VERBATIM):

- secured_creditor : the seller. attrs: legal_basis (SARFAESI|DRT|IBC), bank_name,
  branch, authorised_officer, assignor_bank, trust_name, assignment_date,
  liquidator, court_reference, predecessor_entity, sale_terms, auction_platform_url.
- contact          : a contact point. attrs: phones, email.
- borrower         : one borrower/guarantor. attrs: role, address, lot_index.
- property         : one lot's property block. attrs: property_type, asset_category,
  possession_type, possession_date, construction_type, occupancy_status,
  title_deed_holder, branch_of_lot, address (full property address as written,
  when the notice states one), encumbrance (disclosed charges/dues, e.g. "Nil"
  or a specific charge), lot_index.
- full_description : the SINGLE SOURCE OF TRUTH for one lot's property — the
  COMPLETE description block, copied verbatim as ONE span, from which every
  descriptive field below must be derivable. It MUST contain, when the notice
  states them: the property-type phrase; EVERY survey/plot/patta/flat/door/
  identifier number; the village, taluk and district (and any registration
  district / sub-district); the extent/area; ALL FOUR boundaries AND their
  per-side measurements; and any other descriptive detail (title holder,
  possession, encumbrance, schedules/items). Do NOT stop at the survey numbers —
  run the span through to the end of the boundary/registration text. Copy it
  verbatim with section labels preserved; EXCLUDE only terms-of-sale boilerplate
  (price, EMD, dates, "as is where is"). This span deliberately OVERLAPS the
  granular property/location/extent/boundary/identifier spans (it is their
  UNION, so each of those must fall INSIDE it) — emit BOTH the block and the
  granular spans. Classification attrs (asset_category, a normalised
  property_type) are inferred FROM this text, not required to appear verbatim.
  attrs: lot_index.
- location         : the "Situated At ..." span. attrs: village, taluk, district,
  city, state, area, panchayat, municipality_corporation, ward_no, hobli,
  registration_district, registration_sub_district, landmark, latitude, longitude,
  lot_index.
- identifier       : ONE id span each (emit several). attrs: kind (survey_old|
  survey_new|patta|chitta|khata|property_id|cersai|plot|flat|block|floor|door_old|
  door_new|assessment_old|assessment_new|sale_deed|approved_layout), value, lot_index.
- extent           : an area span. attrs: extent_sqft, total_area,
  super_built_up_area, built_up_area, carpet_area, undivided_share,
  uds_parent_extent, lot_index.
- boundary         : ONE side each. attrs: side (north|south|east|west), adjacency,
  measurement, lot_index.
- schedule         : a sub-parcel (Item/Schedule A,B..). attrs: label, type, extent,
  lot_index.
- auction_terms    : price & dates for a lot. attrs: reserve_price_num, emd_num,
  bid_increment_num, auction_start_dt, auction_end_dt, application_deadline_dt,
  inspection_dt, auto_extension_minutes, sarfaesi_stage, lot_index.
- outstanding      : dues for a lot. attrs: amount_num, as_on, demand_notice_date,
  loan_account_no, lot_index.
- emd_account      : EMD remittance details. attrs: account_name, account_no, ifsc,
  bank, mode_of_payment.
- full_terms       : the COMPLETE "Terms & Conditions of e-Auction" block, copied
  verbatim as a SINGLE notice-level span (NO lot_index): the numbered/bulleted sale
  terms governing the WHOLE notice — EMD refund/forfeiture, deposit schedule (e.g.
  25% immediately + balance in 15 days), who bears stamp duty / registration /
  statutory dues, "AS IS WHERE IS / AS IS WHAT IS", sale confirmation, the
  Authorised Officer's right to postpone/cancel, and the encumbrance disclaimer.
  ONE per notice — a multi-lot notice shares ONE terms block across every lot, so
  emit it ONCE, never per lot. Overlaps emd_account/auction_terms within it; emit both.
- extras           : ONE meaningful fact NOT covered by any class above, as a short
  span. attrs: key (snake_case), value, lot_index (omit if notice-level). USE
  SPARINGLY — only decision-relevant facts a bidder would need: RERA/GST numbers,
  leasehold tenure & NOC/transfer restrictions (e.g. KIADB), IBC option bundles /
  Section 29A eligibility, disclosed tax/maintenance dues, road access, pending
  litigation, machinery lists, per-account outstanding breakdown, TDS-inclusive
  pricing. NEVER for anything that fits an existing class/attr; never restate
  terms-of-sale boilerplate; at most ~5 per notice.

CONVENTIONS:
- A fact that fits none of the classes above goes in `extras` with a snake_case
  `key`.
- extraction_text MUST be copied verbatim from the document (for source
  grounding) AND must be ONE CONTIGUOUS run of characters — a single unbroken
  quote you could select with one drag. NEVER stitch it together from pieces
  found in different places, and never summarise. A joined string matches
  nothing in the document, so the extraction loses its grounding COMPLETELY
  even though every fragment inside it was real.
  When a lot's values are scattered across the notice (typically auction_terms
  and outstanding — price here, dates in a table, deadline in a paragraph),
  quote the ONE run that best anchors the entity, usually the clause or table
  row carrying its principal value, and put every other value in attrs. attrs
  are NOT span-checked, so nothing is lost by doing this — and a shorter honest
  span always beats a longer assembled one.
- Money -> integer rupees in attrs (Rs.9,50,000 -> 950000; "572.34 Lakh" ->
  57234000). Preserve unicode fractions ½ ¼ ¾. Dates ISO 8601 with time when given.
- OMIT any attribute that is absent — NEVER output the string "null"/"NA"/empty.
- For multi-lot notices tag every per-lot entity with lot_index=N.
- For EVERY lot emit (when present) property, full_description, location, extent,
  its identifiers, its boundaries, auction_terms and outstanding — do not stop at
  property_type.

SLOTTING RULES (avoid these common mistakes):
- REGISTRATION DISTRICTS — nearly every Tamil Nadu / Andhra notice closes with
  these and they are HIGH VALUE; do NOT drop the closing clause as boilerplate.
  The clause "within the Registration District of X and the Sub-Registration
  District of Y" (EITHER order) -> emit a SEPARATE location span carrying
  registration_district=X and registration_sub_district=Y. Variants:
  "X Registration District", "Sub Registration District of Y", "S.R.O. Y" /
  "SRO:Y" -> registration_sub_district=Y. Keep BOTH OUT of `district` (the revenue
  district, e.g. Kancheepuram / Chengalpattu) and out of `taluk` — they are the
  separate registration hierarchy. Emit even when they sit in a trailing clause
  after the survey/patta details.
- "X Hobli" -> location hobli=X (Karnataka) — NOT taluk. "X Grama Panchayath" ->
  location panchayat. "within the limits of X Corporation" -> municipality_corporation.
- DRT case refs ("OA No...", "RC No...", "RP No...", "TRC No...") and IBC refs
  ("CP(IB)...", "IA...", NCLT order) -> secured_creditor court_reference — NEVER
  loan_account_no (loan_account_no is the bank loan A/c number).
- ARC notices: the ARC is the seller -> secured_creditor bank_name; the bank the
  debt was assigned FROM -> assignor_bank; the "... Trust" -> trust_name;
  legal_basis stays SARFAESI.
- Survey numbers may be prefixed "R.S No.", "T.S No.", "S.F No.", "Re Sy No.",
  "Old/New S.No." — still emit each as an identifier with kind survey_old/survey_new.
- APARTMENTS / FLATS: set property property_type=flat. Emit the flat number ->
  identifier kind=flat, the floor ("Ground Floor", "2nd Floor") -> identifier
  kind=floor, and the block ("Block No.18") -> identifier kind=block — do NOT
  leave them inside the property blob. A flat owns an UNDIVIDED SHARE (UDS) of
  land: put the share in extent undivided_share and the larger parcel it is carved
  from in extent uds_parent_extent; the flat's own area is built_up_area.
  CRITICAL for flats: the parent-plot / Schedule-A land extent (e.g. "Plot No.3
  measuring an extent of 2257 sq.ft") is the UDS PARENT — record it ONLY in
  uds_parent_extent. NEVER put it in total_area or extent_sqft, and never emit a
  separate extent for it: a flat's headline area is its built_up_area, not the
  whole plot it sits on. total_area/extent_sqft describe the property's OWN land
  (vacant land / a house plot), so a flat generally has neither.
- "Admeasuring ... Northern/Southern/Eastern/Western Side N Feet" gives the
  per-side boundary MEASUREMENT (the dimension) — put N Feet in boundary
  measurement, distinct from adjacency (what abuts that side, e.g. a road/plot).
- "Name of the Title Holder: X" -> property title_deed_holder=X.
- "CERSAI Security Interest Id: N" -> identifier kind=cersai value=N.
- POSSESSION: emit property possession_type ONLY when the notice commits to ONE
  value for that lot. The boilerplate disjunction "Constructive / Symbolic /
  Physical Possession" (in the preamble or a "Type of Possession" column,
  including the unfilled template "(mention whichever is applicable)") names all
  types without choosing — emit NO possession_type for it: not the raw
  disjunction, not a guess. A stated possession DATE ("Possession taken on
  17-07-2025") -> property possession_date (ISO), independent of the type.
- identifier kind MUST be exactly one of the enum values listed above — NEVER
  invent a kind and NEVER copy the document's label as the kind. Map labels:
  "T.S No" / "Sy No" / "S.F No" / "Survey No" / "R.S No" -> survey_old (or
  survey_new when marked new/re-survey); "Re Sy No" / "New S.No" -> survey_new;
  "Block No.18" -> kind=block value=18. The document's label stays only in the
  extraction_text span.
- BOUNDARY MEASUREMENTS: whenever per-side dimensions appear ANYWHERE in the
  description ("East to West on the Northern Side 40 Feet", "Measuring Northern
  Side 34ft", "North to South on the Eastern side: 50 feet"), attach measurement
  to the matching boundary entity — check ALL FOUR sides every time; a notice
  that gives adjacencies usually gives dimensions too, later in the text.

The authoritative field semantics and edge cases (DRT "Upset Price", IBC
liquidators, ARC assignor/trust, column-unit money, etc.) are in the FIELD
CATALOGUE below. The catalogue's nested JSON is a SEMANTICS REFERENCE ONLY —
do NOT output that JSON shape; output only the entity classes above. Map the
catalogue's nested paths onto entity attrs like this:
- property.flat_no/block/floor/plot_no/patta_no/chitta_no/khata_no/
  property_id_no/cersai_id/sale_deed_no/approved_layout_no/survey_numbers/
  door_numbers/assessment_no -> SEPARATE identifier entities (kind=flat|block|
  floor|plot|patta|chitta|khata|property_id|cersai|sale_deed|approved_layout|
  survey_old|survey_new|door_old|door_new|assessment_old|assessment_new),
  NEVER attributes on the property entity.
- property.village/taluk/district/city/area/state/panchayat/
  municipality_corporation/ward_no/hobli/registration_district/
  registration_sub_district/latitude/longitude and landmark -> location attrs.
- property.total_area/extent_sqft/super_built_up_area/built_up_area/
  carpet_area/undivided_share/uds_parent_extent -> extent attrs.
- property.boundaries / boundary_measurements -> boundary entities (adjacency
  vs measurement, one entity per side).
- notice.* -> secured_creditor / contact / property / outstanding / full_terms
  attrs as declared above; auction.* -> auction_terms / emd_account attrs.
- additional_lots -> the same per-lot entities tagged lot_index=N.
- extras -> extras entities. schedules[].description is the schedule span
  itself (no attr needed). enriched_description: NEVER emit — out of scope.

=== FIELD CATALOGUE (extract_enrichment.txt) ===
"""

PROMPT_DESCRIPTION = _LANGEXTRACT_GUIDE + load_canonical_scheme()


# A roster longer than this stops being a scaffold and starts crowding out the
# notice itself; the tail is summarised instead of listed.
MAX_ROSTER_ROWS = 40

#: The portal's own blurb is the field that separates sibling flats sharing a
#: price, a village and a borrower — but it is also the only unbounded one on
#: the row, and 40 of them at full length would swamp the notice. Enough to
#: recognise a lot by, not enough to retell it.
MAX_ROSTER_DESC_CHARS = 140


def _clip(value, limit: int) -> str:
    """One roster field as a single short line — newlines flattened (a row is
    one line by construction) and long text cut on a word boundary."""
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def _roster_row(row: dict) -> str | None:
    """One portal listing as a single compact line, or None when it carries
    nothing worth showing.

    The `listing <aid>:` prefix is the label the model quotes back as
    `portal_aid`; a row without an auction_id renders unlabelled and simply
    cannot be claimed.
    """
    reserve, emd = row.get("reserve"), row.get("emd")
    where = " ".join(str(v).strip() for v in (row.get("village"),
                                              row.get("district")) if v)
    parts: list[str] = []
    if reserve is not None:
        parts.append(f"reserve {int(reserve)}")
    if emd is not None:
        parts.append(f"emd {int(emd)}")
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


def portal_roster_block(roster: list[dict] | None) -> str:
    """Render this notice's portal listings as reference context.

    The auction portal carries its own structured row per lot — reserve price,
    EMD, village, area, property type — scraped independently of the notice
    image. ``pipeline.apply_extractions`` already matches extracted lots back
    onto these rows using exactly those fields; showing them BEFORE extraction
    turns that after-the-fact reconciliation into a segmentation scaffold, so
    the model matches lots it can see against lots known to exist instead of
    guessing where one lot ends and the next begins.

    The one thing the model MAY take from here is the identity of the listing
    itself: `property.portal_aid` names which row a lot is, quoted from the
    `listing <aid>:` label. That is the whole point of showing the roster with
    ids — `match_lots_to_listings` currently infers the same link after the
    fact from reserve-price equality, which cannot decide between lots that
    tie on money, and the model reading both sides at once can. The claim is
    never trusted on its own: the matcher checks it against the price/EMD/
    borrower evidence it already computes and sends a contradiction to the
    human queue rather than writing either answer.

    `portal_aid` is declared HERE rather than in `_LANGEXTRACT_GUIDE` because
    it only exists when a roster does — a notice with no portal rows must not
    be told about an attribute it has no way to fill, and the guide's attrs are
    the ones every extraction can use.

    Two things this block must never become:

    * **A source of values.** Every emitted value has to be a verbatim span of
      the notice — LangExtract grounds each extraction to a character interval,
      so a value copied from here has no honest span and corrupts the grounding
      the whole schema rests on. `portal_aid` is the sole exception and is safe
      precisely because it is not a value: it is an id of the row, carries no
      claim about the property, and attrs are not span-checked.
    * **A lot ordering.** The portal's row order is not the notice's lot order,
      so it cannot be used to assign ``lot_index``.

    Both are stated to the model in the text below. Returns "" when there is
    nothing usable, leaving the prompt byte-identical to before.
    """
    lines = [ln for ln in (_roster_row(r) for r in (roster or [])) if ln]
    if not lines:
        return ""
    shown, hidden = lines[:MAX_ROSTER_ROWS], max(0, len(lines) - MAX_ROSTER_ROWS)
    body = "\n".join(f"  - {ln}" for ln in shown)
    if hidden:
        body += f"\n  - (+{hidden} further listings not shown)"
    return (
        "\n\n=== PORTAL LISTINGS FOR THIS NOTICE (reference only) ===\n"
        f"The auction portal separately lists {len(lines)} lot(s) for this "
        "notice. This is external data scraped from the portal — it is NOT "
        "part of the notice text.\n\n"
        "Use it ONLY to:\n"
        "  - judge where one lot ends and the next begins,\n"
        "  - check you have not merged two lots or split one in half,\n"
        "  - sanity-check a figure you read from the notice, since OCR mangles "
        "digits,\n"
        "  - say WHICH listing each lot is, via property.portal_aid (below).\n\n"
        "NEVER copy a value from this list into your output. Every value you "
        "emit must be text you actually found in the notice and can quote "
        "verbatim from it. Where the notice and this list disagree, extract "
        "what the NOTICE says — the notice is the legal document.\n"
        "The rows are in no particular order: do NOT treat their order as the "
        "notice's lot order and do NOT use it to assign lot_index.\n\n"
        f"{body}\n"
        "\n=== portal_aid: naming the listing ===\n"
        "On EACH lot's `property` entity add the attribute `portal_aid` = the "
        "id from the `listing <id>:` label of the row that lot is — a lot you "
        "read as the row labelled `listing 796269:` gets "
        "portal_aid=\"796269\". This is the ONE thing you may take from this "
        "list, because it identifies a row rather than describing the "
        "property.\n"
        "Rules:\n"
        "  - Decide it from what the NOTICE says about that lot — its reserve "
        "price, EMD, borrower, village, extent, property type — read against "
        "the rows above. Agreement on the money plus one more field is a "
        "match.\n"
        "  - One listing belongs to at most ONE lot. Never put the same "
        "portal_aid on two lots.\n"
        "  - OMIT it when you are not sure, and omit it for every lot that has "
        "no matching row. A missing portal_aid costs nothing — the pipeline "
        "falls back to matching on price. A wrong one names the wrong "
        "property.\n"
        "  - It is checked against the portal's own figures afterwards, so a "
        "guess does not slip through; it is discarded and a human is asked.\n"
    )


def prompt_description_for(expected_lot_count: int | None,
                           roster: list[dict] | None = None) -> str:
    """The per-notice prompt: the shared guide + scheme, plus the reviewer's
    lot count when one exists (Document.expected_lot_count, stamped at the
    classification review gate) and this notice's portal listings when it has
    any. Priming the model with the confirmed count is the recall lever for
    multi-lot notices — it knows when it has found them all and when it has
    invented extras; the roster then tells it what those lots look like.
    Neither present -> unchanged prompt."""
    roster_block = portal_roster_block(roster)
    if expected_lot_count is None:
        return PROMPT_DESCRIPTION + roster_block
    n = int(expected_lot_count)
    if n <= 1:
        hint = (
            "\n\nA human reviewer confirmed this notice sells EXACTLY ONE lot. "
            "Extract every entity for that single lot; multiple schedules or "
            "items sharing one reserve price are parts of the same lot, not "
            "separate lots."
        )
    else:
        hint = (
            f"\n\nA human reviewer confirmed this notice sells EXACTLY {n} "
            f"lots. Extract entities for ALL {n} lots, stamping lot_index 1 "
            f"through {n} — do not merge distinct lots and do not invent "
            "extras beyond the confirmed count."
        )
    return PROMPT_DESCRIPTION + hint + roster_block


def E(cls, text, **attrs):
    """Terse Extraction builder."""
    return lx.data.Extraction(extraction_class=cls, extraction_text=text,
                              attributes={k: v for k, v in attrs.items()
                                          if v is not None})


# --------------------------------------------------------------------------- #
# Example 1 — SINGLE notice (one lot, two sub-items). Source: 736547.
# Example text is built from verbatim phrases of the real notice so every
# extraction_text is a substring.
# --------------------------------------------------------------------------- #
SINGLE_TEXT = (
    "Authorised Officer of Bank of Baroda, Secured Creditor, will be sold on "
    "\"As is where is\", \"As is what is\" and \"without recourse\" basis. "
    "M/s Health Mushrooms D.No.519/4, Keelakkarai, Perambalur - 621 219. "
    "1. Mrs Suganthi Johnpeter (Proprietor) No 6, Indira Nagar, Elambalur. "
    "2. Mr Johnpeter Sebastian (Guarantor) No 4, Indira Nagar, Elambalur. "
    "Equitable mortgage of vacant land located in UDR SF No 256/1F, SF No 390/1, "
    "Plot No 4, Perambalur North Village, Perambalur Taluk and District. "
    "Item No 1 : An extent of East West 30 feet on both sides, North South Eastern "
    "site 40 feet, Western side 28 ¼ feet, admeasuring an extent of 1023 ¼ Square "
    "feet (95.11 Square meters) having the following four boundaries : East of Plot "
    "No 5, West of Plot No 1 belonged to Kowsalya and Varatharajan, South of Plot "
    "belongs to Gomathi W/o Vijayakumar, North of 2nd item. "
    "Item No 2 : An extent of 218 ¼ square feet (20.32 Square meters). "
    "The total extent of above two items of plots are 1242 ¼ Square feet (115.43 "
    "Square Meters). "
    "Dues as on 26.03.2026 Cumulative Total Dues of Rs 53,91,240.72. "
    "Date & Time of E-auction 14.05.2026 14.00 to 18.00. "
    "1.Rs.9,50,000/- 2.Rs.95,000/- 3.Rs.25,000/-. PhysicalPossession. "
    "Property Inspection date & Time 13.05.2026 11.00 to 16.00. "
    "online auction portal https://baanknet.com. prospective bidders may contact "
    "the Authorised officer on Tel No. 04328 - 225080. DATE : 26.03.2026"
)

SINGLE_EXAMPLE = lx.data.ExampleData(
    text=SINGLE_TEXT,
    extractions=[
        E("secured_creditor", "Bank of Baroda", legal_basis="SARFAESI",
          bank_name="Bank of Baroda", branch="Perambalur",
          sale_terms="As is where is, As is what is, without recourse",
          auction_platform_url="https://baanknet.com"),
        E("contact", "Tel No. 04328 - 225080", phones="04328-225080"),
        E("borrower", "M/s Health Mushrooms", role="borrower", lot_index="1",
          address="D.No.519/4, Keelakkarai, Perambalur - 621 219"),
        E("borrower", "Mrs Suganthi Johnpeter (Proprietor)", role="proprietor",
          lot_index="1", address="No 6, Indira Nagar, Elambalur"),
        E("borrower", "Mr Johnpeter Sebastian (Guarantor)", role="guarantor",
          lot_index="1", address="No 4, Indira Nagar, Elambalur"),
        E("property", "Equitable mortgage of vacant land located in UDR SF No "
          "256/1F, SF No 390/1, Plot No 4", lot_index="1",
          property_type="vacant land", asset_category="immovable",
          possession_type="physical"),
        E("full_description", "Equitable mortgage of vacant land located in UDR "
          "SF No 256/1F, SF No 390/1, Plot No 4, Perambalur North Village, "
          "Perambalur Taluk and District. Item No 1 : An extent of East West 30 "
          "feet on both sides, North South Eastern site 40 feet, Western side 28 ¼ "
          "feet, admeasuring an extent of 1023 ¼ Square feet (95.11 Square meters) "
          "having the following four boundaries : East of Plot No 5, West of Plot "
          "No 1 belonged to Kowsalya and Varatharajan, South of Plot belongs to "
          "Gomathi W/o Vijayakumar, North of 2nd item. Item No 2 : An extent of "
          "218 ¼ square feet (20.32 Square meters). The total extent of above two "
          "items of plots are 1242 ¼ Square feet (115.43 Square Meters).",
          lot_index="1"),
        E("location", "Perambalur North Village, Perambalur Taluk and District",
          lot_index="1", village="Perambalur North", taluk="Perambalur",
          district="Perambalur"),
        E("identifier", "UDR SF No 256/1F", kind="survey_new", value="256/1F",
          lot_index="1"),
        E("identifier", "SF No 390/1", kind="survey_old", value="390/1",
          lot_index="1"),
        E("identifier", "Plot No 4", kind="plot", value="4", lot_index="1"),
        E("extent", "1242 ¼ Square feet (115.43 Square Meters)", lot_index="1",
          extent_sqft="1242.25", total_area="1242 ¼ sq.ft (115.43 sq.m)"),
        E("schedule", "Item No 1 : An extent of East West 30 feet on both sides, "
          "North South Eastern site 40 feet, Western side 28 ¼ feet, admeasuring "
          "an extent of 1023 ¼ Square feet (95.11 Square meters)", lot_index="1",
          label="Item 1", type="land", extent="1023 ¼ sq.ft (95.11 sq.m)"),
        E("schedule", "Item No 2 : An extent of 218 ¼ square feet (20.32 Square "
          "meters)", lot_index="1", label="Item 2", type="land",
          extent="218 ¼ sq.ft (20.32 sq.m)"),
        E("boundary", "East of Plot No 5", side="east", adjacency="Plot No 5",
          measurement="30 feet", lot_index="1"),
        E("boundary", "West of Plot No 1 belonged to Kowsalya and Varatharajan",
          side="west", adjacency="Plot No 1 (Kowsalya & Varatharajan)",
          measurement="28 ¼ feet", lot_index="1"),
        E("boundary", "South of Plot belongs to Gomathi W/o Vijayakumar",
          side="south", adjacency="Plot of Gomathi W/o Vijayakumar", lot_index="1"),
        E("boundary", "North of 2nd item", side="north", adjacency="2nd item",
          lot_index="1"),
        E("auction_terms", "1.Rs.9,50,000/- 2.Rs.95,000/- 3.Rs.25,000/-",
          lot_index="1", reserve_price_num="950000", emd_num="95000",
          bid_increment_num="25000", auction_start_dt="2026-05-14T14:00",
          auction_end_dt="2026-05-14T18:00", inspection_dt="2026-05-13T11:00"),
        E("outstanding", "Dues as on 26.03.2026 Cumulative Total Dues of Rs "
          "53,91,240.72", lot_index="1", amount_num="5391240.72",
          as_on="2026-03-26"),
    ],
)

# --------------------------------------------------------------------------- #
# Example 2 — MULTI notice (lots 1-2 annotated; model generalises). Source: 738029.
# --------------------------------------------------------------------------- #
MULTI_TEXT = (
    "Authorized Officer of Equitas small finance Bank, Secured Creditor, will be "
    "sold on \"As is where is\", \"As is what is\". "
    "For details and queries contact no- Sathish 9940286237. "
    "1. Ponniyammal M 2. Munusamy A (residing at Gummidipoondi, Tamil Nadu, 601201). "
    "All That Piece And Parcel Of Land And Building, Comprised In S.Nos.108/6A, "
    "99/13, As Per Patta No.96, New S.No.99/13A, & 108/6A, With An Extent Of 1305 "
    "Sq.Ft., Situated At Penia Chozhiyampakkam Village, Gummidipoondi Taluk, "
    "Thiruvallur District. Rs.10,68,000/- Rs.1,06,800/- Rs.10,000/-. 11.05.2026 "
    "From 11.00 AM to 12.30 PM. Loan Account No:-700006541659 (Total Outstanding "
    "being Rs.8,12,695/- as on 24.03.2026). "
    "Mr/Mrs. Indhra D Mr/Mrs. V Ananthi (residing at No.44, Lakshmi koiil Street, "
    "Gummidipoodi, Tamil Nadu, 601201). All That Piece And Parcel Of Land And "
    "Building, Comprised In S.Nos.41/28, 25/4, With An Extent Of 1526 Sq.Ft., "
    "Situated At Chinna Chozhiyambampakkam Village, Gummidipoodi Taluk, Thiruvallur "
    "District And Bounded On: (North By)- Pathway (South By)- Land Belongs To "
    "Mr.Govindhan (East By)- Land Belongs To Mr.Murugan (West By)- Land Belongs To "
    "Mr.Aadhiappan Reddy. Rs.7,73,000/-. 11.05.2026 From 11.00 AM to 12.30 PM. "
    "Loan Account No:- 700009454343 (Total Outstanding being Rs.6,84,413/- as on "
    "24.03.2026). "
    "The intending purchaser is required to submit EMD by way of NEFT/RTGS/DD in "
    "the account of \"Equitas Small Finance Bank Ltd\" Account No- 200000807725 and "
    "IFSC code- ESFB0001001 on or before date: 08.05.2026. "
    "Terms & Conditions of E-Auction: 1. The property is sold on \"As is where "
    "is\", \"As is what is\" and \"whatever there is\" basis. 2. EMD shall be "
    "refunded to unsuccessful bidders without interest. 3. The successful bidder "
    "shall pay 25% of the sale price (less EMD) immediately and the balance 75% "
    "within 15 days, failing which the amount already paid shall be forfeited and "
    "the property re-auctioned. 4. The successful bidder shall bear the stamp duty, "
    "registration charges and all statutory dues / taxes. 5. The sale is subject "
    "to confirmation by the Secured Creditor and the Authorised Officer reserves "
    "the right to accept or reject any or all bids or to postpone / cancel the "
    "auction without assigning any reason. 6. The property is sold subject to all "
    "known and unknown encumbrances; bidders should make their own enquiries "
    "before bidding."
)

MULTI_EXAMPLE = lx.data.ExampleData(
    text=MULTI_TEXT,
    extractions=[
        E("secured_creditor", "Equitas small finance Bank", legal_basis="SARFAESI",
          bank_name="Equitas Small Finance Bank",
          sale_terms="As is where is, As is what is"),
        E("contact", "Sathish 9940286237", phones="9940286237"),
        # ---- Lot 1 ----
        E("borrower", "Ponniyammal M", role="borrower", lot_index="1",
          address="Gummidipoondi, Tamil Nadu, 601201"),
        E("borrower", "Munusamy A", role="co-borrower", lot_index="1"),
        E("property", "All That Piece And Parcel Of Land And Building, Comprised In "
          "S.Nos.108/6A, 99/13, As Per Patta No.96, New S.No.99/13A, & 108/6A",
          lot_index="1", property_type="land and building",
          asset_category="immovable"),
        E("full_description", "All That Piece And Parcel Of Land And Building, "
          "Comprised In S.Nos.108/6A, 99/13, As Per Patta No.96, New S.No.99/13A, "
          "& 108/6A, With An Extent Of 1305 Sq.Ft., Situated At Penia "
          "Chozhiyampakkam Village, Gummidipoondi Taluk, Thiruvallur District.",
          lot_index="1"),
        E("location", "Penia Chozhiyampakkam Village, Gummidipoondi Taluk, "
          "Thiruvallur District", lot_index="1", village="Penia Chozhiyampakkam",
          taluk="Gummidipoondi", district="Thiruvallur"),
        E("identifier", "Patta No.96", kind="patta", value="96", lot_index="1"),
        E("identifier", "S.Nos.108/6A, 99/13", kind="survey_old",
          value="108/6A, 99/13", lot_index="1"),
        E("identifier", "New S.No.99/13A, & 108/6A", kind="survey_new",
          value="99/13A, 108/6A", lot_index="1"),
        E("extent", "1305 Sq.Ft.", lot_index="1", extent_sqft="1305",
          total_area="1305 sq.ft"),
        E("auction_terms", "Rs.10,68,000/- Rs.1,06,800/- Rs.10,000/-", lot_index="1",
          reserve_price_num="1068000", emd_num="106800", bid_increment_num="10000",
          auction_start_dt="2026-05-11T11:00", auction_end_dt="2026-05-11T12:30"),
        E("outstanding", "Loan Account No:-700006541659 (Total Outstanding being "
          "Rs.8,12,695/- as on 24.03.2026)", lot_index="1", amount_num="812695",
          as_on="2026-03-24", loan_account_no="700006541659"),
        # ---- Lot 2 ----
        E("borrower", "Indhra D", role="borrower", lot_index="2",
          address="No.44, Lakshmi koiil Street, Gummidipoodi, Tamil Nadu, 601201"),
        E("borrower", "V Ananthi", role="co-borrower", lot_index="2"),
        E("property", "All That Piece And Parcel Of Land And Building, Comprised In "
          "S.Nos.41/28, 25/4", lot_index="2", property_type="land and building",
          asset_category="immovable"),
        E("full_description", "All That Piece And Parcel Of Land And Building, "
          "Comprised In S.Nos.41/28, 25/4, With An Extent Of 1526 Sq.Ft., Situated "
          "At Chinna Chozhiyambampakkam Village, Gummidipoodi Taluk, Thiruvallur "
          "District And Bounded On: (North By)- Pathway (South By)- Land Belongs To "
          "Mr.Govindhan (East By)- Land Belongs To Mr.Murugan (West By)- Land "
          "Belongs To Mr.Aadhiappan Reddy.", lot_index="2"),
        E("location", "Chinna Chozhiyambampakkam Village, Gummidipoodi Taluk, "
          "Thiruvallur District", lot_index="2",
          village="Chinna Chozhiyambampakkam", taluk="Gummidipoodi",
          district="Thiruvallur"),
        E("identifier", "S.Nos.41/28, 25/4", kind="survey_old", value="41/28, 25/4",
          lot_index="2"),
        E("extent", "1526 Sq.Ft.", lot_index="2", extent_sqft="1526",
          total_area="1526 sq.ft"),
        E("boundary", "(North By)- Pathway", side="north", adjacency="Pathway",
          lot_index="2"),
        E("boundary", "(South By)- Land Belongs To Mr.Govindhan", side="south",
          adjacency="Land of Mr.Govindhan", lot_index="2"),
        E("boundary", "(East By)- Land Belongs To Mr.Murugan", side="east",
          adjacency="Land of Mr.Murugan", lot_index="2"),
        E("boundary", "(West By)- Land Belongs To Mr.Aadhiappan Reddy", side="west",
          adjacency="Land of Mr.Aadhiappan Reddy", lot_index="2"),
        E("auction_terms", "Rs.7,73,000/-", lot_index="2",
          reserve_price_num="773000", auction_start_dt="2026-05-11T11:00",
          auction_end_dt="2026-05-11T12:30"),
        E("outstanding", "Loan Account No:- 700009454343 (Total Outstanding being "
          "Rs.6,84,413/- as on 24.03.2026)", lot_index="2", amount_num="684413",
          as_on="2026-03-24", loan_account_no="700009454343"),
        # ---- notice-level EMD account ----
        E("emd_account", "Account No- 200000807725 and IFSC code- ESFB0001001",
          account_name="Equitas Small Finance Bank Ltd", account_no="200000807725",
          ifsc="ESFB0001001", mode_of_payment="NEFT/RTGS/DD"),
        # ---- notice-level terms & conditions (ONE block, shared by both lots) ----
        E("full_terms", "Terms & Conditions of E-Auction: 1. The property is sold "
          "on \"As is where is\", \"As is what is\" and \"whatever there is\" "
          "basis. 2. EMD shall be refunded to unsuccessful bidders without "
          "interest. 3. The successful bidder shall pay 25% of the sale price (less "
          "EMD) immediately and the balance 75% within 15 days, failing which the "
          "amount already paid shall be forfeited and the property re-auctioned. "
          "4. The successful bidder shall bear the stamp duty, registration charges "
          "and all statutory dues / taxes. 5. The sale is subject to confirmation "
          "by the Secured Creditor and the Authorised Officer reserves the right to "
          "accept or reject any or all bids or to postpone / cancel the auction "
          "without assigning any reason. 6. The property is sold subject to all "
          "known and unknown encumbrances; bidders should make their own enquiries "
          "before bidding."),
    ],
)

# --------------------------------------------------------------------------- #
# Example 3 — APARTMENT / FLAT with UDS (one lot). Source: Canara Bank, Kolathur.
# Flats carry detail vacant land does not: Flat No / Floor / Block identifiers, an
# UNDIVIDED SHARE (UDS) of land carved from a larger parent extent, per-side
# boundary MEASUREMENTS distinct from adjacency, a named title-deed holder, and a
# CERSAI security-interest id. Neither land example above demonstrates these, so
# this one teaches them. Built from verbatim phrases of the real notice.
# --------------------------------------------------------------------------- #
APARTMENT_TEXT = (
    "Authorised Officer of Canara Bank, Kolathur, Chennai - 600099. "
    "Mob: 9944838284, Email: cb16062@canarabank.com. "
    "Borrower Name & Address: 1. Mr. K. Yoganand, S/o. Mr. Kamalanathan, "
    "2. Mrs. D. S. Kaavya, W/o. Mr. K. Yoganand, Both are residing at: 56A, 120A, "
    "BB Road, 4th Lane, Vyasarpadi, Chennai - 600039. Outstanding Amount: "
    "Rs.34,18,676.36/- as on 31.03.2026. "
    "DETAILS OF PROPERTY: Name of the Title Holder: Mr. K. Yoganand & Mrs. D S "
    "Kaavya & CERSAI Security Interest Id: 400038860000 All the piece and parcel "
    "of land and building bearing, Sub divided as Plot No.4, Annapoorna Nagar, "
    "Madhavaram, Chennai, comprised in Survey No.1258/2 of No.50, Madhavaram "
    "Village, Chennai District, land measuring an extent of 380 Sq.ft, Undivided "
    "Share of Land out of total extent of 2400 Sq.ft., out of 9600 Sq ft., "
    "together with Flat No. G1, Ground Floor, Block No.18, having a built up area "
    "of 805 Sq ft., (including common area) and Bounded on the North by : 24 Feet "
    "Road, South by : Plot New No.5, Old No.1 (Sub-division), East by : Plot New "
    "No.17, Old No.3 (Sub-division), West by : Plot New No.19, Old No.3 "
    "(Sub-division), Admeasuring East to West on the Northern Side 40 Feet, East "
    "to West on the Southern Side 40 Feet, North to South on the Eastern Side 60 "
    "Feet, North to South on the Western Side 60 Feet, The above the Property "
    "within the Sub-Registration District of Madhavaram and Registration District "
    "of Chennai North."
)

APARTMENT_EXAMPLE = lx.data.ExampleData(
    text=APARTMENT_TEXT,
    extractions=[
        E("secured_creditor", "Canara Bank", legal_basis="SARFAESI",
          bank_name="Canara Bank", branch="Kolathur"),
        E("contact", "Mob: 9944838284, Email: cb16062@canarabank.com",
          phones="9944838284", email="cb16062@canarabank.com"),
        E("borrower", "Mr. K. Yoganand, S/o. Mr. Kamalanathan", role="borrower",
          lot_index="1", address="56A, 120A, BB Road, 4th Lane, Vyasarpadi, "
          "Chennai - 600039"),
        E("borrower", "Mrs. D. S. Kaavya, W/o. Mr. K. Yoganand", role="co-borrower",
          lot_index="1"),
        E("property", "All the piece and parcel of land and building bearing, Sub "
          "divided as Plot No.4, Annapoorna Nagar, Madhavaram, Chennai",
          lot_index="1", property_type="flat", asset_category="immovable",
          title_deed_holder="Mr. K. Yoganand & Mrs. D S Kaavya"),
        E("full_description", "All the piece and parcel of land and building "
          "bearing, Sub divided as Plot No.4, Annapoorna Nagar, Madhavaram, "
          "Chennai, comprised in Survey No.1258/2 of No.50, Madhavaram Village, "
          "Chennai District, land measuring an extent of 380 Sq.ft, Undivided "
          "Share of Land out of total extent of 2400 Sq.ft., out of 9600 Sq ft., "
          "together with Flat No. G1, Ground Floor, Block No.18, having a built up "
          "area of 805 Sq ft., (including common area) and Bounded on the North "
          "by : 24 Feet Road, South by : Plot New No.5, Old No.1 (Sub-division), "
          "East by : Plot New No.17, Old No.3 (Sub-division), West by : Plot New "
          "No.19, Old No.3 (Sub-division), Admeasuring East to West on the "
          "Northern Side 40 Feet, East to West on the Southern Side 40 Feet, North "
          "to South on the Eastern Side 60 Feet, North to South on the Western "
          "Side 60 Feet, The above the Property within the Sub-Registration "
          "District of Madhavaram and Registration District of Chennai North.",
          lot_index="1"),
        E("location", "Madhavaram Village, Chennai District", lot_index="1",
          village="Madhavaram", district="Chennai"),
        E("location", "Sub-Registration District of Madhavaram and Registration "
          "District of Chennai North", lot_index="1",
          registration_sub_district="Madhavaram",
          registration_district="Chennai North"),
        E("identifier", "CERSAI Security Interest Id: 400038860000", kind="cersai",
          value="400038860000", lot_index="1"),
        E("identifier", "Plot No.4", kind="plot", value="4", lot_index="1"),
        E("identifier", "Survey No.1258/2 of No.50", kind="survey_new",
          value="1258/2", lot_index="1"),
        E("identifier", "Flat No. G1", kind="flat", value="G1", lot_index="1"),
        E("identifier", "Ground Floor", kind="floor", value="Ground", lot_index="1"),
        E("identifier", "Block No.18", kind="block", value="18", lot_index="1"),
        E("extent", "380 Sq.ft", lot_index="1", undivided_share="380 Sq.ft"),
        E("extent", "Undivided Share of Land out of total extent of 2400 Sq.ft., "
          "out of 9600 Sq ft.", lot_index="1",
          uds_parent_extent="2400 Sq.ft (out of 9600 Sq.ft)"),
        E("extent", "built up area of 805 Sq ft., (including common area)",
          lot_index="1", built_up_area="805 Sq.ft (including common area)",
          extent_sqft="805"),
        E("boundary", "North by : 24 Feet Road", side="north",
          adjacency="24 Feet Road", measurement="40 Feet", lot_index="1"),
        E("boundary", "South by : Plot New No.5, Old No.1 (Sub-division)",
          side="south", adjacency="Plot New No.5, Old No.1 (Sub-division)",
          measurement="40 Feet", lot_index="1"),
        E("boundary", "East by : Plot New No.17, Old No.3 (Sub-division)",
          side="east", adjacency="Plot New No.17, Old No.3 (Sub-division)",
          measurement="60 Feet", lot_index="1"),
        E("boundary", "West by : Plot New No.19, Old No.3 (Sub-division)",
          side="west", adjacency="Plot New No.19, Old No.3 (Sub-division)",
          measurement="60 Feet", lot_index="1"),
        E("outstanding", "Outstanding Amount: Rs.34,18,676.36/- as on 31.03.2026",
          lot_index="1", amount_num="3418676.36", as_on="2026-03-31"),
    ],
)

# --------------------------------------------------------------------------- #
# Example 4 — DRT sale (one lot). Source: Indian Bank / DRT-III Chennai, 750600.
# legal_basis=DRT notices carry a TWO-LEVEL case reference (tribunal-level TRC No.
# + case-level OA No.) that prior examples never demonstrated, so the model was
# dropping court_reference despite the prose rule. Also teaches the DRT EMD
# remittance shape (bank-transfer emd_account, distinct from the Equitas example's
# single-account-line phrasing) and "leave emd_num null when only a % of the
# upset price is stated, not a rupee figure" (no invented math).
# --------------------------------------------------------------------------- #
DRT_TEXT = (
    "DEBTS RECOVERY TRIBUNAL - III, CHENNAI. TRC No.578/2023. E-AUCTION SALE "
    "Dated:17.04.2026. "
    "The under mentioned property will be sold by online E-Auction through "
    "website https://www.bankacquisitions.com for recovery of a sum of "
    "Rs.7,94,16,566.37 as on 19.02.2026 from M/s. Sai Baba Lamination & 2 Others "
    "payable to Indian Bank, Guindy Branch in OA No.419/2017. "
    "DESCRIPTION OF PROPERTY: All that piece and parcel of land of an extent of "
    "2.35 Acres comprised in Punja Old Survey No.183/1B, Survey No.183/1B1A1, "
    "No.62, Nynarkuppam Village, Mudafryarkuppam Revenue Village, Idaikazhinadu "
    "Town Panchayat, Cheiyur Taluk, Kancheepuram District. Bounded on the - North "
    "by: Punja land of Mr. S. Arumugam, South by: Punja land of Saroja and "
    "Velayutha Pillai, East by: Punja land of Muthu Chettiar, West by: Punja land "
    "of Muthu. "
    "Upset Price Rs.2,00,00,000/-. Date and time of e-auction 29.05.2026 between "
    "1100 hours and 1200 hours. Earnest Money Deposit 10% of the upset price. "
    "EMD on or before 26.05.2026 through NEFT/RTGS in favour of \"The Recovery "
    "Officer, DRT-3, Chennai\" to A/c No.163302000000250, Indian Overseas Bank, "
    "Cathedral Branch, IFSC Code: IOBA0000109. Bid Increment Minimum "
    "Rs.5,00,000/-. Inspection of Property 14.05.2026 from 11.00 a.m. to 3.00 "
    "p.m. RECOVERY OFFICER E. SASIKUMAR."
)

DRT_EXAMPLE = lx.data.ExampleData(
    text=DRT_TEXT,
    extractions=[
        E("secured_creditor", "Indian Bank, Guindy Branch in OA No.419/2017",
          legal_basis="DRT", bank_name="Indian Bank", branch="Guindy",
          authorised_officer="E. Sasikumar",
          court_reference="TRC No.578/2023; OA No.419/2017"),
        E("borrower", "M/s. Sai Baba Lamination & 2 Others", role="borrower",
          lot_index="1"),
        E("property", "All that piece and parcel of land of an extent of 2.35 "
          "Acres comprised in Punja Old Survey No.183/1B, Survey No.183/1B1A1, "
          "No.62", lot_index="1", property_type="land", asset_category="immovable"),
        E("full_description", "All that piece and parcel of land of an extent of "
          "2.35 Acres comprised in Punja Old Survey No.183/1B, Survey "
          "No.183/1B1A1, No.62, Nynarkuppam Village, Mudafryarkuppam Revenue "
          "Village, Idaikazhinadu Town Panchayat, Cheiyur Taluk, Kancheepuram "
          "District. Bounded on the - North by: Punja land of Mr. S. Arumugam, "
          "South by: Punja land of Saroja and Velayutha Pillai, East by: Punja "
          "land of Muthu Chettiar, West by: Punja land of Muthu.", lot_index="1"),
        E("location", "Nynarkuppam Village, Mudafryarkuppam Revenue Village, "
          "Idaikazhinadu Town Panchayat, Cheiyur Taluk, Kancheepuram District",
          lot_index="1", village="Nynarkuppam", taluk="Cheiyur",
          district="Kancheepuram", panchayat="Idaikazhinadu Town Panchayat"),
        E("identifier", "Punja Old Survey No.183/1B", kind="survey_old",
          value="183/1B", lot_index="1"),
        E("identifier", "Survey No.183/1B1A1", kind="survey_new",
          value="183/1B1A1", lot_index="1"),
        E("extent", "2.35 Acres", lot_index="1", total_area="2.35 Acres"),
        E("boundary", "North by: Punja land of Mr. S. Arumugam", side="north",
          adjacency="Punja land of Mr. S. Arumugam", lot_index="1"),
        E("boundary", "South by: Punja land of Saroja and Velayutha Pillai",
          side="south", adjacency="Punja land of Saroja and Velayutha Pillai",
          lot_index="1"),
        E("boundary", "East by: Punja land of Muthu Chettiar", side="east",
          adjacency="Punja land of Muthu Chettiar", lot_index="1"),
        E("boundary", "West by: Punja land of Muthu", side="west",
          adjacency="Punja land of Muthu", lot_index="1"),
        E("auction_terms", "Upset Price Rs.2,00,00,000/-", lot_index="1",
          reserve_price_num="20000000", bid_increment_num="500000",
          auction_start_dt="2026-05-29T11:00", auction_end_dt="2026-05-29T12:00",
          inspection_dt="2026-05-14T11:00",
          application_deadline_dt="2026-05-26"),
        E("outstanding", "recovery of a sum of Rs.7,94,16,566.37 as on "
          "19.02.2026", lot_index="1", amount_num="79416566.37",
          as_on="2026-02-19"),
        E("emd_account", "A/c No.163302000000250, Indian Overseas Bank, "
          "Cathedral Branch, IFSC Code: IOBA0000109",
          account_name="The Recovery Officer, DRT-3, Chennai",
          account_no="163302000000250",
          bank="Indian Overseas Bank, Cathedral Branch", ifsc="IOBA0000109",
          mode_of_payment="NEFT/RTGS"),
    ],
)

# --------------------------------------------------------------------------- #
# Example 5 — ARC assignment, apartment/UDS (one lot). Source: Omkara ARC /
# IndusInd Bank, 747290. The seller is an Asset Reconstruction Company holding
# debt assigned FROM the original lender under a Trust — prior examples only ever
# showed a bank as its own secured_creditor, so assignor_bank/trust_name were
# never demonstrated and the model dropped them despite the prose rule.
# --------------------------------------------------------------------------- #
ARC_TEXT = (
    "OMKARA ASSETS RECONSTRUCTION PVT. LTD. PUBLIC NOTICE FOR E-AUCTION SALE OF "
    "IMMOVABLE PROPERTY. E-Auction Sale Notice under the SARFAESI Act, 2002. "
    "Possession taken by the Authorised Officer of Omkara Assets Reconstruction "
    "Pvt Ltd (OARPL). Omkara Assets Reconstruction Pvt Ltd (OARPL), acting in its "
    "capacity as Trustee of Omkara PS 06/2021-22 Trust, has acquired entire "
    "outstanding debts of the below accounts vide Assignment Agreement dated "
    "25.06.2021 from IndusInd Bank Limited (IBL) (Assignor Bank). "
    "Name of Borrower & Co Borrower: MR. N Elango (Borrower) and Mrs. Aruna E "
    "(Co-borrower). Sale Deed Document No.4845/2005 dated 01.12.2005 of SRO "
    "Kodambakkam. "
    "All that piece and parcel of Residential Flat, bearing Flat No. E, Ground "
    "Floor, Priya Apartments, Old Door No.105, New Door No.192, Rangarajapuram "
    "Main Road, Kodambakkam, Chennai - 600024, having built up area of 500 Sq.ft "
    "together with 331 Sq.ft of Undivided Share of Land, out of the total land "
    "measuring 3 Ground and 744 Sq.ft, comprised in T S No.34, Block No.44 "
    "situated at No. 109, Puliyur Village, Egmore-Nungambakkam Taluk, Chennai "
    "District, bounded on the North by: Door No.104 comprised in T S No.33, "
    "South by: Door No.106, West by: property owned by Mrs. Zita Aruliah. "
    "Situated within the Sub Registration District of Kodambakkam and "
    "Registration District of Central Chennai. "
    "13(2) Notice Date 20.04.2022. Physical Possession Date 31.12.2025. "
    "Outstanding due as on 15.04.2026 Rs.37,42,152/-. Reserve Price "
    "Rs.33,00,000/-."
)

ARC_EXAMPLE = lx.data.ExampleData(
    text=ARC_TEXT,
    extractions=[
        E("secured_creditor", "Omkara Assets Reconstruction Pvt Ltd (OARPL)",
          legal_basis="SARFAESI", bank_name="Omkara Assets Reconstruction Pvt Ltd",
          assignor_bank="IndusInd Bank", trust_name="Omkara PS 06/2021-22 Trust",
          assignment_date="2021-06-25"),
        E("borrower", "MR. N Elango (Borrower)", role="borrower", lot_index="1"),
        E("borrower", "Mrs. Aruna E (Co-borrower)", role="co-borrower",
          lot_index="1"),
        E("property", "All that piece and parcel of Residential Flat, bearing "
          "Flat No. E, Ground Floor, Priya Apartments", lot_index="1",
          property_type="flat", asset_category="immovable",
          possession_type="physical", possession_date="2025-12-31",
          address="Flat No. E, Ground Floor, Priya Apartments, Old Door "
          "No.105, New Door No.192, Rangarajapuram Main Road, Kodambakkam, "
          "Chennai - 600024"),
        E("full_description", "All that piece and parcel of Residential Flat, "
          "bearing Flat No. E, Ground Floor, Priya Apartments, Old Door No.105, "
          "New Door No.192, Rangarajapuram Main Road, Kodambakkam, Chennai - "
          "600024, having built up area of 500 Sq.ft together with 331 Sq.ft of "
          "Undivided Share of Land, out of the total land measuring 3 Ground and "
          "744 Sq.ft, comprised in T S No.34, Block No.44 situated at No. 109, "
          "Puliyur Village, Egmore-Nungambakkam Taluk, Chennai District, bounded "
          "on the North by: Door No.104 comprised in T S No.33, South by: Door "
          "No.106, West by: property owned by Mrs. Zita Aruliah.", lot_index="1"),
        E("location", "Puliyur Village, Egmore-Nungambakkam Taluk, Chennai "
          "District", lot_index="1", village="Puliyur",
          taluk="Egmore-Nungambakkam", district="Chennai", city="Chennai"),
        E("location", "Sub Registration District of Kodambakkam and "
          "Registration District of Central Chennai", lot_index="1",
          registration_sub_district="Kodambakkam",
          registration_district="Central Chennai"),
        E("identifier", "Sale Deed Document No.4845/2005 dated 01.12.2005",
          kind="sale_deed", value="4845/2005", lot_index="1"),
        E("identifier", "Old Door No.105", kind="door_old", value="105",
          lot_index="1"),
        E("identifier", "New Door No.192", kind="door_new", value="192",
          lot_index="1"),
        E("identifier", "T S No.34", kind="survey_old", value="34",
          lot_index="1"),
        E("identifier", "Block No.44", kind="block", value="44", lot_index="1"),
        E("identifier", "Flat No. E", kind="flat", value="E", lot_index="1"),
        E("identifier", "Ground Floor", kind="floor", value="Ground",
          lot_index="1"),
        E("extent", "built up area of 500 Sq.ft", lot_index="1",
          built_up_area="500 Sq.ft", extent_sqft="500"),
        E("extent", "331 Sq.ft of Undivided Share of Land", lot_index="1",
          undivided_share="331 Sq.ft"),
        E("extent", "total land measuring 3 Ground and 744 Sq.ft", lot_index="1",
          uds_parent_extent="3 Ground and 744 Sq.ft"),
        E("boundary", "North by: Door No.104 comprised in T S No.33",
          side="north", adjacency="Door No.104 (T S No.33)", lot_index="1"),
        E("boundary", "South by: Door No.106", side="south",
          adjacency="Door No.106", lot_index="1"),
        E("boundary", "West by: property owned by Mrs. Zita Aruliah",
          side="west", adjacency="property of Mrs. Zita Aruliah", lot_index="1"),
        E("outstanding", "Outstanding due as on 15.04.2026 Rs.37,42,152/-",
          lot_index="1", amount_num="3742152", as_on="2026-04-15",
          demand_notice_date="2022-04-20"),
        E("auction_terms", "Reserve Price Rs.33,00,000/-", lot_index="1",
          reserve_price_num="3300000"),
    ],
)

# --------------------------------------------------------------------------- #
# Example 6 — KARNATAKA / leasehold industrial (one lot). Source: Karnataka Bank,
# 752691. Teaches the Karnataka geo hierarchy (hobli — previously prose-only and
# suppressed by the Gemini example-derived schema), a COMMITTED possession_type
# with a possession_date, auto_extension_minutes, partner-role borrowers, and the
# `extras` class: leasehold tenure, a KIADB NOC use-restriction, and
# TDS-inclusive pricing — decision-relevant facts with no dedicated field.
# --------------------------------------------------------------------------- #
KARNATAKA_TEXT = (
    "Notice is hereby given to public in general and in particular to Borrower "
    "(s) and Guarantor (s) that the below described leasehold immovable property "
    "mortgaged/charged to the secured creditor, the constructive possession of "
    "which has been taken by the Authorised Officer of Karnataka Bank Ltd, the "
    "Secured Creditor on 17-07-2025, will be sold on \"As is Where is\", and "
    "\"As is What is\" basis on 08.05.2026 for recovery of total amount of "
    "Rs.1,62,24,218.05 (i.e. in respect of PSTL A/c No. 1827001800243301) + "
    "interest from 05-07-2025 and costs due to the Karnataka Bank Ltd, "
    "Davangere - Main Branch. "
    "Name & Address of the Borrower / Co-obligants / Guarantors: 1) M/s, Texas "
    "Textile Represented by its partners : a) Sri Hitesh Y, b) Smt. Nirmala Raj, "
    "addressed at: No.3547/78A, Balaji Layout, Kundawada Road, Near Kundawada "
    "Lake, Devaraj Urs Layout, Devanagere-577006. "
    "Description of the Immovable Property: All that part and parcel of "
    "Industrial Land bearing Plot No. 56, in Sy. No. 5 & 13 measuring 21775.57 "
    "Sq. Feet with building constructed thereon situated at KIADB, Karur "
    "Industrial area, Karur Village, Kasaba Hobli, Davangere and bounded by: "
    "East: Plot No.55, West: Plot No.57 & 57-A, North: Road, South: Plot No.45 "
    "& 46. "
    "Reserve Price Rs 3,40,12,000/- (Including TDS @1%). EMD: Rs. 34,01,200. "
    "Date & Time of Auction: 08.05.2026 from 11.05 A.M to 11.25 A.M. The "
    "E-auction will be conducted through portal www.auctionbazaar.com with "
    "unlimited extension of 05 minutes. "
    "The intending bidder shall use the demised premises only for the purpose "
    "of Readymade Garments' Textile Industry as per NOC dated 25-09-2025 issued "
    "by KADB. "
    "Address of the Secured Creditor Karnataka Bank Ltd, Davangere Main Branch "
    "(Phone: 08192-258432(G), 9449595580 (BM), 9448497320(ABM))."
)

KARNATAKA_EXAMPLE = lx.data.ExampleData(
    text=KARNATAKA_TEXT,
    extractions=[
        E("secured_creditor", "Karnataka Bank Ltd, Davangere - Main Branch",
          legal_basis="SARFAESI", bank_name="Karnataka Bank Ltd",
          branch="Davangere - Main",
          sale_terms="As is Where is, As is What is",
          auction_platform_url="www.auctionbazaar.com"),
        E("contact", "Phone: 08192-258432(G), 9449595580 (BM), 9448497320(ABM)",
          phones="08192-258432, 9449595580, 9448497320"),
        E("borrower", "M/s, Texas Textile", role="borrower", lot_index="1",
          address="No.3547/78A, Balaji Layout, Kundawada Road, Near Kundawada "
          "Lake, Devaraj Urs Layout, Devanagere-577006"),
        E("borrower", "Sri Hitesh Y", role="partner", lot_index="1"),
        E("borrower", "Smt. Nirmala Raj", role="partner", lot_index="1"),
        E("property", "All that part and parcel of Industrial Land bearing "
          "Plot No. 56, in Sy. No. 5 & 13", lot_index="1",
          property_type="industrial land", asset_category="immovable",
          possession_type="constructive", possession_date="2025-07-17"),
        E("full_description", "All that part and parcel of Industrial Land "
          "bearing Plot No. 56, in Sy. No. 5 & 13 measuring 21775.57 Sq. Feet "
          "with building constructed thereon situated at KIADB, Karur "
          "Industrial area, Karur Village, Kasaba Hobli, Davangere and bounded "
          "by: East: Plot No.55, West: Plot No.57 & 57-A, North: Road, South: "
          "Plot No.45 & 46.", lot_index="1"),
        E("location", "KIADB, Karur Industrial area, Karur Village, Kasaba "
          "Hobli, Davangere", lot_index="1", village="Karur", hobli="Kasaba",
          district="Davangere", area="KIADB, Karur Industrial area"),
        E("identifier", "Plot No. 56", kind="plot", value="56", lot_index="1"),
        E("identifier", "Sy. No. 5 & 13", kind="survey_old", value="5 & 13",
          lot_index="1"),
        E("extent", "21775.57 Sq. Feet", lot_index="1",
          total_area="21775.57 sq.ft", extent_sqft="21775.57"),
        E("boundary", "East: Plot No.55", side="east", adjacency="Plot No.55",
          lot_index="1"),
        E("boundary", "West: Plot No.57 & 57-A", side="west",
          adjacency="Plot No.57 & 57-A", lot_index="1"),
        E("boundary", "North: Road", side="north", adjacency="Road",
          lot_index="1"),
        E("boundary", "South: Plot No.45 & 46", side="south",
          adjacency="Plot No.45 & 46", lot_index="1"),
        E("auction_terms", "Reserve Price Rs 3,40,12,000/- (Including TDS "
          "@1%). EMD: Rs. 34,01,200", lot_index="1",
          reserve_price_num="34012000", emd_num="3401200",
          auction_start_dt="2026-05-08T11:05", auction_end_dt="2026-05-08T11:25",
          auto_extension_minutes="5"),
        E("outstanding", "recovery of total amount of Rs.1,62,24,218.05 (i.e. "
          "in respect of PSTL A/c No. 1827001800243301) + interest from "
          "05-07-2025", lot_index="1", amount_num="16224218.05",
          as_on="2025-07-05", loan_account_no="1827001800243301"),
        # extras: decision-relevant facts with no dedicated field.
        E("extras", "leasehold immovable property", key="tenure",
          value="leasehold"),
        E("extras", "The intending bidder shall use the demised premises only "
          "for the purpose of Readymade Garments' Textile Industry as per NOC "
          "dated 25-09-2025 issued by KADB", key="leasehold_use_restriction",
          value="KIADB NOC 25-09-2025: use restricted to Readymade Garments' "
          "Textile Industry"),
        E("extras", "(Including TDS @1%)", key="reserve_price_tds",
          value="reserve price includes TDS @1%"),
    ],
)

# --------------------------------------------------------------------------- #
# Example 7 — CAN FIN multi-lot with DISJUNCTIVE possession. Source: Can Fin
# Homes / Salem, CANFN17791720254760.png (not in the gold set). The "Type of
# Possession" column reads "constructive / physical" — the notice never commits
# to one value, so property entities carry NO possession_type (user decision:
# null over raw disjunction or a guess; evals/langextract_gold.py EXPECT_NULL).
# Also demonstrates: encumbrance ("known encumbrances : NIL"), Salem-style
# registration hierarchy inside the description, patta subdivision surveys, and
# the ½ unicode fraction in extents.
# --------------------------------------------------------------------------- #
CANFIN_TEXT = (
    "NOTICE is hereby given to the public in general and in particular to the "
    "Borrower (s) and Guarantor (s) that the below described immovable "
    "properties mortgaged/charged to the Secured Creditor, the possession of "
    "which has been taken by the Authorised Officer of Can Fin Homes Ltd., "
    "Salem Branch, will be sold by holding e-auction on \"As is where is\", "
    "\"As is what is\", and \"Whatever there is\" on 19.06.2026. "
    "Sl. No. 1. Mrs. Sarun Saroja I and Mr. Iruthalyaraj L (Borrowers) and "
    "Guarantors Mrs. M.Preethi and Mr. M.Mahendran. Liability as on 18-05-2026 "
    "Rs.12,80,926/-. Reserve Price Rs.32,00,000/-. Earnest Money Deposit "
    "Rs.3,20,000/-. Type of Possession: constructive / physical. Description "
    "of the property: All the piece and parcel of land situated in Salem West "
    "registration District, Omakur Sub registration District, Kadalyampatty "
    "Taluk, Kanjanaaickenpatty Village, Government re-survey no.400/2B, as per "
    "New Survey subdivision, Patta No.1724, Survey no.400/2B1a1A, in this an "
    "Area of 20021⁄2 Sq. feet of House site which Is Situated Within the "
    "Following Boundaries : To the North of 20 Feet Width Road, To the South "
    "of Remaining Land of executant Mrs. Devaki, To the East of Saarun Saroja "
    "Land - Plot No. 71, To the West of Saarun Saroja Land. known "
    "encumbrances : NIL. "
    "Sl. No. 2. Selvakumar K, S/o. Krishnamoorthy, Shanthi S, W/o. Selvakumar "
    "K (Borrowers). Liability as on 18-05-2026 Rs.37,05,426/-. Reserve Price "
    "Rs.37,00,000/-. Earnest Money Deposit Rs.3,70,000/-. Type of Possession: "
    "constructive / physical. Description of the property: Salem west "
    "registration district, Salem east joint 1 SRD, Salem taluk, Chinnanur "
    "village, patta no.247, S.no.18/3, as per patta sub division S.no.18/3A1, "
    "out of the above land within 1769.50 sq.ft of land are related to this "
    "description. the boundaries measurements for the same as per deed no. "
    "4659/2013 are below To North of : Rangasamy & Senthil And Other Property, "
    "To South of : 15 Feet Wide Road East West Road, To East of : Senthil @ "
    "Subramani Land, To West of : Veerasamy Mariyammal Land. known "
    "encumbrances : NIL. "
    "Link for Participating e-auction: www.auctionbazaar.com"
)

CANFIN_EXAMPLE = lx.data.ExampleData(
    text=CANFIN_TEXT,
    extractions=[
        E("secured_creditor", "Can Fin Homes Ltd., Salem Branch",
          legal_basis="SARFAESI", bank_name="Can Fin Homes Ltd",
          branch="Salem",
          sale_terms="As is where is, As is what is, Whatever there is",
          auction_platform_url="www.auctionbazaar.com"),
        # ---- Lot 1 ----
        E("borrower", "Mrs. Sarun Saroja I", role="borrower", lot_index="1"),
        E("borrower", "Mr. Iruthalyaraj L", role="co-borrower", lot_index="1"),
        E("borrower", "Mrs. M.Preethi", role="guarantor", lot_index="1"),
        E("borrower", "Mr. M.Mahendran", role="guarantor", lot_index="1"),
        # "constructive / physical" commits to nothing -> NO possession_type.
        E("property", "All the piece and parcel of land situated in Salem "
          "West registration District, Omakur Sub registration District, "
          "Kadalyampatty Taluk, Kanjanaaickenpatty Village", lot_index="1",
          property_type="house site", asset_category="immovable",
          encumbrance="Nil"),
        E("full_description", "All the piece and parcel of land situated in "
          "Salem West registration District, Omakur Sub registration "
          "District, Kadalyampatty Taluk, Kanjanaaickenpatty Village, "
          "Government re-survey no.400/2B, as per New Survey subdivision, "
          "Patta No.1724, Survey no.400/2B1a1A, in this an Area of "
          "20021⁄2 Sq. feet of House site which Is Situated Within the "
          "Following Boundaries : To the North of 20 Feet Width Road, To the "
          "South of Remaining Land of executant Mrs. Devaki, To the East of "
          "Saarun Saroja Land - Plot No. 71, To the West of Saarun Saroja "
          "Land.", lot_index="1"),
        E("location", "Salem West registration District, Omakur Sub "
          "registration District, Kadalyampatty Taluk, Kanjanaaickenpatty "
          "Village", lot_index="1", registration_district="Salem West",
          registration_sub_district="Omakur", taluk="Kadalyampatty",
          village="Kanjanaaickenpatty"),
        E("identifier", "Government re-survey no.400/2B", kind="survey_old",
          value="400/2B", lot_index="1"),
        E("identifier", "Patta No.1724", kind="patta", value="1724",
          lot_index="1"),
        E("identifier", "Survey no.400/2B1a1A", kind="survey_new",
          value="400/2B1a1A", lot_index="1"),
        E("extent", "20021⁄2 Sq. feet", lot_index="1",
          total_area="2002½ sq.ft", extent_sqft="2002.5"),
        E("boundary", "To the North of 20 Feet Width Road", side="north",
          adjacency="20 Feet Width Road", lot_index="1"),
        E("boundary", "To the South of Remaining Land of executant Mrs. "
          "Devaki", side="south",
          adjacency="Remaining Land of executant Mrs. Devaki", lot_index="1"),
        E("boundary", "To the East of Saarun Saroja Land - Plot No. 71",
          side="east", adjacency="Saarun Saroja Land - Plot No. 71",
          lot_index="1"),
        E("boundary", "To the West of Saarun Saroja Land", side="west",
          adjacency="Saarun Saroja Land", lot_index="1"),
        E("auction_terms", "Reserve Price Rs.32,00,000/-. Earnest Money "
          "Deposit Rs.3,20,000/-", lot_index="1", reserve_price_num="3200000",
          emd_num="320000", auction_start_dt="2026-06-19"),
        E("outstanding", "Liability as on 18-05-2026 Rs.12,80,926/-",
          lot_index="1", amount_num="1280926", as_on="2026-05-18"),
        # ---- Lot 2 ----
        E("borrower", "Selvakumar K, S/o. Krishnamoorthy", role="borrower",
          lot_index="2"),
        E("borrower", "Shanthi S, W/o. Selvakumar K", role="co-borrower",
          lot_index="2"),
        E("property", "Salem west registration district, Salem east joint 1 "
          "SRD, Salem taluk, Chinnanur village, patta no.247, S.no.18/3",
          lot_index="2", property_type="land", asset_category="immovable",
          encumbrance="Nil"),
        E("full_description", "Salem west registration district, Salem east "
          "joint 1 SRD, Salem taluk, Chinnanur village, patta no.247, "
          "S.no.18/3, as per patta sub division S.no.18/3A1, out of the above "
          "land within 1769.50 sq.ft of land are related to this description. "
          "the boundaries measurements for the same as per deed no. 4659/2013 "
          "are below To North of : Rangasamy & Senthil And Other Property, To "
          "South of : 15 Feet Wide Road East West Road, To East of : Senthil "
          "@ Subramani Land, To West of : Veerasamy Mariyammal Land.",
          lot_index="2"),
        E("location", "Salem west registration district, Salem east joint 1 "
          "SRD, Salem taluk, Chinnanur village", lot_index="2",
          registration_district="Salem West",
          registration_sub_district="Salem East Joint 1", taluk="Salem",
          village="Chinnanur"),
        E("identifier", "patta no.247", kind="patta", value="247",
          lot_index="2"),
        E("identifier", "S.no.18/3", kind="survey_old", value="18/3",
          lot_index="2"),
        E("identifier", "S.no.18/3A1", kind="survey_new", value="18/3A1",
          lot_index="2"),
        E("identifier", "deed no. 4659/2013", kind="sale_deed",
          value="4659/2013", lot_index="2"),
        E("extent", "1769.50 sq.ft", lot_index="2", total_area="1769.50 sq.ft",
          extent_sqft="1769.5"),
        E("boundary", "To North of : Rangasamy & Senthil And Other Property",
          side="north", adjacency="Rangasamy & Senthil And Other Property",
          lot_index="2"),
        E("boundary", "To South of : 15 Feet Wide Road East West Road",
          side="south", adjacency="15 Feet Wide Road East West Road",
          lot_index="2"),
        E("boundary", "To East of : Senthil @ Subramani Land", side="east",
          adjacency="Senthil @ Subramani Land", lot_index="2"),
        E("boundary", "To West of : Veerasamy Mariyammal Land", side="west",
          adjacency="Veerasamy Mariyammal Land", lot_index="2"),
        E("auction_terms", "Reserve Price Rs.37,00,000/-. Earnest Money "
          "Deposit Rs.3,70,000/-", lot_index="2", reserve_price_num="3700000",
          emd_num="370000", auction_start_dt="2026-06-19"),
        E("outstanding", "Liability as on 18-05-2026 Rs.37,05,426/-",
          lot_index="2", amount_num="3705426", as_on="2026-05-18"),
    ],
)

EXAMPLES = [SINGLE_EXAMPLE, MULTI_EXAMPLE, APARTMENT_EXAMPLE, DRT_EXAMPLE,
            ARC_EXAMPLE, KARNATAKA_EXAMPLE, CANFIN_EXAMPLE]


_MODEL_CACHE: dict = {}
_REASONING_OFF_CLASS = None


def _reasoning_off_model_cls():
    """Lazily build (once) an OpenAILanguageModel subclass that injects a fixed
    ``extra_body`` into every Chat Completions request.

    langextract's OpenAI provider whitelists the params it forwards and drops
    both ``extra_body`` and a ``reasoning`` object, so a hybrid-reasoning model
    (DeepSeek V4) can't otherwise be told to turn reasoning OFF for this
    copy-the-spans task. Overriding the request builder is the reliable seam
    (``extra_body`` is a first-class OpenAI-SDK arg that OpenRouter honours)."""
    global _REASONING_OFF_CLASS
    if _REASONING_OFF_CLASS is None:
        from langextract.providers.openai import OpenAILanguageModel

        class _ReasoningOffOpenAI(OpenAILanguageModel):
            def _build_chat_completions_params(self, prompt, config):
                params = super()._build_chat_completions_params(prompt, config)
                forced = getattr(self, "_forced_extra_body", None)
                if forced:
                    params["extra_body"] = {**params.get("extra_body", {}), **forced}
                return params

        _REASONING_OFF_CLASS = _ReasoningOffOpenAI
    return _REASONING_OFF_CLASS


def _openrouter_model(model_id: str | None = None, reasoning_off: bool = False):
    """Cached OpenAI-compatible model pointed at OpenRouter.

    ``model_id`` overrides the default (env LANGEXTRACT_MODEL_ID) so callers can
    route per notice type; the cache is keyed by (model_id, reasoning_off) so
    several models coexist in one process. ``reasoning_off`` forces provider-side
    reasoning off for that model (see ``_reasoning_off_model_cls``).

    An unrouted call falls back to OPENROUTER_MODEL_EXTRACT_SINGLE — the model
    the routing config actually names for a notice of unknown shape. It used to
    fall back to `google/gemini-2.5-flash`, which is in the config for the chat
    agent and the dossier classifier and was never chosen for extraction: a
    caller that omitted `model_id` silently overrode a measured decision with a
    model nobody had measured. It showed up as bursts of 8 chunk calls returning
    10 tokens each — the "Content must contain an 'extractions' key" failure,
    from a model asked a question its config never intended for it.
    """
    from langextract.providers.openai import OpenAILanguageModel
    from pipeline.config import OPENROUTER_MODEL_EXTRACT_SINGLE
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    model_id = model_id or os.environ.get("LANGEXTRACT_MODEL_ID",
                                          OPENROUTER_MODEL_EXTRACT_SINGLE)
    cache_key = (model_id, reasoning_off)
    if cache_key not in _MODEL_CACHE:
        base_url = os.environ.get("OPENROUTER_BASE_URL",
                                  "https://openrouter.ai/api/v1")
        if reasoning_off:
            model = _reasoning_off_model_cls()(
                model_id=model_id, api_key=key, base_url=base_url, max_workers=4)
            model._forced_extra_body = {"reasoning": {"enabled": False}}
        else:
            model = OpenAILanguageModel(
                model_id=model_id, api_key=key, base_url=base_url, max_workers=4)
        # langextract constructs its OpenAI client with no timeout, so it inherits
        # the SDK default (600s read x max_retries=2) — and with extraction_passes=2
        # on top, ONE dead socket can hold a worker for the better part of an hour.
        # When the connection dies for a reason that will never resolve (laptop
        # sleep, network drop) the call never returns, and in a bounded thread pool
        # each such worker is gone for good: throughput decays to zero while the run
        # still looks alive. An explicit deadline turns that into a fast [fail] the
        # resume pass picks up. Kwargs can't carry this — OpenAILanguageModel stores
        # **kwargs in _extra_kwargs and never forwards them to the client — so
        # rebuild the client instead.
        import openai
        model._client = openai.OpenAI(
            api_key=key, base_url=base_url,
            timeout=float(os.environ.get("LANGEXTRACT_REQUEST_TIMEOUT_S", "300")),
            max_retries=1)
        _MODEL_CACHE[cache_key] = model
    return _MODEL_CACHE[cache_key]


def extract(markdown: str, model_id: str | None = None,
            reasoning_off: bool = False,
            expected_lot_count: int | None = None,
            roster: list[dict] | None = None):
    """Run LangExtract over one notice's MinerU markdown.

    ``expected_lot_count`` — the reviewer-confirmed lot count from the
    classification gate, injected into the prompt so the model knows how many
    lots to find (see prompt_description_for). None leaves the prompt as-is.

    ``roster`` — this notice's portal listings ({reserve, emd, village,
    district, area, ptype} per lot), injected as reference context so the model
    can segment the notice against lots already known to exist. Reference only:
    the block tells the model never to copy a value from it (see
    portal_roster_block). None/empty leaves the prompt as-is.

    Provider is env-driven via LANGEXTRACT_PROVIDER:
      'openrouter' (default) -> OpenRouter, OpenAI-compatible; model
          ``model_id`` if given else env LANGEXTRACT_MODEL_ID (default
          'google/gemini-2.5-flash'), key OPENROUTER_API_KEY. Shares the
          gateway/billing with the rest of the repo.
      'gemini'               -> Gemini API direct; model default 'gemini-2.5-flash',
          key LANGEXTRACT_API_KEY.
    ``model_id`` lets the caller route per notice type (single vs multi — see
    pipeline/extract_routing); ``reasoning_off`` forces provider-side reasoning
    off for hybrid-reasoning models (OpenRouter path only). passes
    (LANGEXTRACT_PASSES, default 2) maximises multi-lot recall; results carry
    char_interval source grounding either way.

    Both paths run WITHOUT schema constraints: on the gemini path langextract
    would otherwise derive a response schema from EXAMPLES and silently suppress
    any attr key not demonstrated there, while the OpenRouter path never
    constrains — so evals would test different behaviour than production. Set
    LANGEXTRACT_USE_SCHEMA=1 to restore constrained generation on gemini.
    """
    from pipeline.extract_routing import char_buffer_for
    common = dict(
        text_or_documents=markdown,
        prompt_description=prompt_description_for(expected_lot_count, roster),
        examples=EXAMPLES, extraction_passes=int(os.environ.get("LANGEXTRACT_PASSES", "2")),
        max_char_buffer=char_buffer_for(markdown), max_workers=4,
    )
    if os.environ.get("LANGEXTRACT_PROVIDER", "openrouter").lower() == "openrouter":
        return lx.extract(model=_openrouter_model(model_id, reasoning_off),
                          fence_output=True, use_schema_constraints=False, **common)
    use_schema = os.environ.get("LANGEXTRACT_USE_SCHEMA", "").strip() == "1"
    return lx.extract(
        model_id=model_id or os.environ.get("LANGEXTRACT_MODEL_ID", "gemini-2.5-flash"),
        api_key=os.environ.get("LANGEXTRACT_API_KEY"),
        fence_output=not use_schema,
        use_schema_constraints=use_schema, **common)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    md = open(sys.argv[1], encoding="utf-8").read()
    result = extract(md)
    for e in result.extractions:
        loc = getattr(e, "char_interval", None)
        print(f"[{e.extraction_class}] {e.extraction_text[:60]!r} @ {loc}  "
              f"{e.attributes}")
    # Optional: lx.visualize(result) -> interactive HTML for the review queue.
