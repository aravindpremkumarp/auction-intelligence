"""The form the model fills in: one Pydantic model per read.

Design rules, each enforced by a test in tests/pipeline/test_reader_schema.py:

* Every value is a VERBATIM QUOTE from the text (``quote`` / ``*_quote``).
  Numbers, dates and areas are parsed from the quote by pipeline/reader/
  normalize.py — the model never does arithmetic or unit conversion.
* A key fact carries a ``status``: ``found`` with a quote, ``not_stated`` when
  the lot does not state it, ``illegible`` when the text is there but the OCR
  mangled it. "Prefer not_stated over inference" is the rule the prompt
  repeats; a place part (district, state, ...) is kept downstream only when it
  occurs in the lot's own text.
* Long blocks (the property description, the terms) are given as ANCHORS —
  the first and last few words — and code takes the span between them. A
  copied 2,000-character block is exactly what the old reader got wrong.
* Enums are lenient on input (``Flat`` -> ``flat``; an unknown value -> None)
  and strict on output. ``extra="ignore"`` so a model that adds a field does
  not fail the parse.
* ``render_catalogue()`` prints the schema as the field catalogue the prompt
  carries; pipeline/prompts/extract_enrichment.txt is regenerated from it
  (PR5), so this file is the single source of truth for what is extracted.

SCHEMA_VERSION bumps whenever a field's meaning or name changes; stored
entities carry it and pipeline/reader/migrate.py brings old blobs forward.
"""
from __future__ import annotations

import json
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 1

Status = Literal["found", "not_stated", "illegible"]
Unit = Literal["rupees", "lakh", "crore"]
Event = Literal["auction_start", "auction_end", "application_deadline",
                "inspection", "possession_date", "outstanding_as_on",
                "assignment_date", "demand_notice_date", "notice_date"]
ExtentRole = Literal["total_area", "built_up_area", "super_built_up_area",
                     "carpet_area", "undivided_share", "uds_parent_extent"]
Side = Literal["north", "south", "east", "west"]
Possession = Literal["symbolic", "physical", "constructive"]
LegalBasis = Literal["SARFAESI", "DRT", "IBC", "other"]

QUOTE_DESC = ("ONE contiguous verbatim run of the text (at most ~300 characters) "
              "that carries the value. Copy exactly; never join pieces from "
              "different places, never paraphrase.")


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


def _lenient_literal(*fields: str):
    """Lowercase + map unknown values to None for Literal-typed fields."""
    def _v(cls, v, info):
        if v is None:
            return None
        allowed = _literal_values(cls.model_fields[info.field_name].annotation)
        s = str(v).strip()
        for cand in (s, s.lower(), s.upper(), s.replace(" ", "_").lower()):
            if cand in allowed:
                return cand
        return None
    return field_validator(*fields, mode="before")(_v)


def _literal_values(annotation) -> set:
    """All literal values inside an annotation like ``Literal[...] | None``."""
    out: set = set()
    if get_origin(annotation) is Literal:
        out.update(get_args(annotation))
    else:
        for a in get_args(annotation):
            out |= _literal_values(a)
    return out


class KeyFact(_Model):
    """A fact the lot must state; the model may say it does not."""
    status: Status = Field("not_stated", description=(
        "found = the lot states it (give the quote); not_stated = the lot "
        "does not state it — do NOT infer it; illegible = the text is there "
        "but garbled by OCR (give the garbled quote)."))
    quote: str | None = Field(None, description=QUOTE_DESC)

    _lenient = _lenient_literal("status")


class Money(KeyFact):
    unit: Unit | None = Field(None, description=(
        "The unit the figure is stated in: rupees, lakh or crore. Read it from "
        "the figure itself ('Rs.70.00 Lakhs') or from the column header "
        "('Reserve Price (In Lakhs)'). Leave empty when unknown."))

    _lenient_unit = _lenient_literal("unit")


class DateFact(KeyFact):
    event: Event = Field(..., description=(
        "Which event the date belongs to. auction_start / auction_end are the "
        "e-auction window; application_deadline is the last date for EMD / "
        "bid submission; inspection is the property-inspection date."))
    iso: str | None = Field(None, description=(
        "The date as YYYY-MM-DD, with THH:MM when a time is stated. Every "
        "digit group must come from the quote; code discards it otherwise."))


class Anchors(_Model):
    """Start and end of a long block, so code can cut the span itself."""
    first_words: str = Field(..., description=(
        "The first 4–12 words of the block, verbatim, exactly as they appear."))
    last_words: str = Field(..., description=(
        "The last 4–12 words of the block, verbatim, exactly as they appear."))


class Identifier(_Model):
    kind: str = Field(..., description=(
        "One of: survey_old, survey_new, patta, chitta, khata, property_id, "
        "cersai, plot, flat, block, floor, door_old, door_new, assessment_old, "
        "assessment_new, sale_deed, approved_layout. 'T.S No' / 'S.F No' / "
        "'R.S No' / 'Sy No' are survey_old; 'Re Sy No' / 'New S.No' are "
        "survey_new; 'Block No.18' is kind=block value=18."))
    value: str = Field(..., description="The number itself, with its subdivision "
                                       "suffix (72/1B), without the label.")
    quote: str = Field(..., description=QUOTE_DESC)


class Extent(_Model):
    role: ExtentRole = Field(..., description=(
        "total_area = the land / plot extent of a plot or house; built_up_area "
        "(incl. plinth area) / super_built_up_area / carpet_area = a building's "
        "own area; undivided_share = a flat's UDS of land; uds_parent_extent = "
        "the whole plot the UDS is carved from (NEVER a flat's total_area)."))
    quote: str = Field(..., description="The area with its unit, verbatim: "
                                       "'1987.50 sq.ft', '2 acres 35 cents', "
                                       "'0.49.5 Hectares'. Keep ½ ¼ ¾.")

    _lenient_role = _lenient_literal("role")


class Boundary(_Model):
    side: Side
    adjacency_quote: str | None = Field(None, description=(
        "WHAT lies on that side, verbatim ('Road', 'Plot of Mr.X')."))
    measurement_quote: str | None = Field(None, description=(
        "The DIMENSION along that side, verbatim ('40 Feet', '12.19 m')."))

    _lenient_side = _lenient_literal("side")


class Place(_Model):
    """Where the lot is. Every part must appear in the lot's own text."""
    quote: str = Field(..., description="The 'situated at ...' run, verbatim.")
    village: str | None = None
    taluk: str | None = Field(None, description="Taluk / Tehsil / Taluka.")
    district: str | None = Field(None, description=(
        "The REVENUE district, only when the text names it as the district. "
        "Never infer it from a village or city name."))
    registration_district: str | None = Field(None, description=(
        "'within the Registration District of X'. Not the revenue district."))
    registration_sub_district: str | None = Field(None, description=(
        "'Sub-Registration District of Y', 'S.R.O. Y', 'SRO: Y'."))
    hobli: str | None = Field(None, description="'X Hobli' (Karnataka). Not a taluk.")
    panchayat: str | None = Field(None, description="'X Grama Panchayath'.")
    municipality_corporation: str | None = Field(None, description=(
        "'within the limits of X Corporation / Municipality'."))
    city: str | None = None
    area: str | None = Field(None, description="Locality / nagar / layout name.")
    state: str | None = Field(None, description="Only when the text names it.")
    ward_no: str | None = None
    landmark: str | None = None


class Party(_Model):
    name_quote: str = Field(..., description="The person's or firm's name, verbatim.")
    role: str | None = Field(None, description=(
        "borrower | co-borrower | guarantor | mortgagor | director | partner | "
        "proprietor | legal-heir, as the text labels them."))
    address_quote: str | None = Field(None, description="Their address, verbatim.")


class Schedule(_Model):
    """A sub-parcel of one lot: Schedule A/B/C, Item 1/2."""
    label: str = Field(..., description="'A', 'B', 'Item 1' ...")
    type: str | None = Field(None, description="land | building | uds | flat | machinery")
    extent_quote: str | None = Field(None, description="Its extent with unit, verbatim.")
    anchors: Anchors | None = Field(None, description="Where the schedule's text starts and ends.")


class Extra(_Model):
    key: str = Field(..., description="snake_case name of a decision-relevant fact "
                                     "no other field covers (rera_no, leasehold_tenure, "
                                     "road_access, pending_litigation ...).")
    value_quote: str = Field(..., description=QUOTE_DESC)


class LotRead(_Model):
    """Everything one auction lot states about itself."""
    lot_label: str | None = Field(None, description=(
        "The lot's own number as printed ('1', 'Lot 2', 'Sl.No.3'), if any."))
    portal_aid: str | None = Field(None, description=(
        "When a portal roster is given: the roster row this lot is, by its aid."))
    borrowers: list[Party] = Field(default_factory=list)
    property_type: KeyFact = Field(default_factory=KeyFact, description=(
        "The property-type phrase as written ('Residential flat', 'Vacant land')."))
    property_type_norm: str | None = Field(None, description=(
        "One of flat | house | villa | plot | land | commercial | industrial | "
        "godown | agricultural, chosen FROM the quote. A flat/apartment is 'flat'."))
    possession: KeyFact = Field(default_factory=KeyFact, description=(
        "The sentence that states which possession was taken for THIS lot. A "
        "menu 'Constructive / Symbolic / Physical' that does not choose is "
        "not_stated."))
    possession_type: Possession | None = Field(None, description=(
        "symbolic | physical | constructive, only when the quote commits to ONE."))
    reserve_price: Money = Field(default_factory=Money, description=(
        "Reserve price / Upset price of THIS lot, as a quote with its unit."))
    emd: Money = Field(default_factory=Money, description="Earnest Money Deposit of THIS lot.")
    bid_increment: Money | None = None
    dates: list[DateFact] = Field(default_factory=list, description=(
        "Every date this lot's own text states, one entry per event. Dates "
        "stated once for the whole notice go in the notice read instead."))
    inspection: KeyFact | None = Field(None, description="Inspection date/time text.")
    outstanding: Money | None = Field(None, description="The dues demanded, with unit.")
    loan_account_nos: list[str] = Field(default_factory=list)
    location: Place | None = None
    identifiers: list[Identifier] = Field(default_factory=list)
    extents: list[Extent] = Field(default_factory=list)
    boundaries: list[Boundary] = Field(default_factory=list)
    schedules: list[Schedule] = Field(default_factory=list)
    description: Anchors | None = Field(None, description=(
        "The COMPLETE property-description block of this lot: from the "
        "property-type / 'All that piece and parcel' phrase through every "
        "survey number, the village/taluk/district, the extent, all four "
        "boundaries and the registration district clause. Give only where it "
        "starts and ends; do not copy it."))
    title_deed_holder_quote: str | None = None
    encumbrance_quote: str | None = Field(None, description="Known encumbrances / dues, or 'Nil'.")
    construction_type: str | None = Field(None, description="'RCC', 'tiled roof', ... as written.")
    occupancy_status: str | None = Field(None, description="vacant | tenanted | self-occupied | under-construction")
    branch_of_lot: str | None = Field(None, description="The branch this lot belongs to, in a multi-branch notice.")
    extras: list[Extra] = Field(default_factory=list, description="At most 5.")

    _lenient_poss = _lenient_literal("possession_type")

    @field_validator("extras")
    @classmethod
    def _cap_extras(cls, v):
        return v[:5]


class SegmentRead(_Model):
    """What one chunk of text (a few lots, with the notice head and tail as
    context) says. ``lots`` holds ONLY the lots whose description sits in the
    chunk — never the context's."""
    lots: list[LotRead] = Field(default_factory=list)


class Contact(_Model):
    quote: str = Field(..., description=QUOTE_DESC)
    phones: list[str] = Field(default_factory=list)
    email: str | None = None


class EmdAccount(_Model):
    quote: str = Field(..., description=QUOTE_DESC)
    account_name: str | None = None
    account_no: str | None = None
    ifsc: str | None = None
    bank: str | None = None
    mode_of_payment: str | None = Field(None, description="'RTGS/NEFT', 'Demand Draft' ...")


class SharedTerms(_Model):
    """Facts the notice states ONCE for every lot."""
    dates: list[DateFact] = Field(default_factory=list)
    possession: KeyFact | None = Field(None, description=(
        "A possession statement that applies to every lot ('the physical "
        "possession of which has been taken'), if the notice makes one."))
    possession_type: Possession | None = None

    _lenient_poss = _lenient_literal("possession_type")


class NoticeRead(_Model):
    """What the notice states about itself, once (head and tail)."""
    legal_basis: LegalBasis | None = Field(None, description=(
        "SARFAESI (authorised officer of a bank / ARC / NBFC), DRT (Recovery "
        "Officer; price often 'Upset Price'), IBC (a liquidator sells under an "
        "NCLT order)."))
    bank_name_quote: str | None = Field(None, description=(
        "The secured creditor — the seller — as named. For an ARC notice the "
        "ARC, not the original lender."))
    branch: str | None = None
    authorised_officer: str | None = None
    assignor_bank: str | None = Field(None, description="The lender the debt was assigned FROM (ARC notices).")
    trust_name: str | None = Field(None, description="The ARC trust, e.g. 'ACRE 166 Trust'.")
    court_reference: str | None = Field(None, description=(
        "OA / RC / RP / TRC No. (DRT) or CP(IB) / IA / NCLT order (IBC). Never a loan account."))
    liquidator: str | None = None
    predecessor_entity: str | None = None
    sale_terms_quote: str | None = Field(None, description="'As is where is, as is what is ...'")
    auction_platform_url: str | None = None
    contacts: list[Contact] = Field(default_factory=list)
    emd_account: EmdAccount | None = None
    terms: Anchors | None = Field(None, description=(
        "The complete 'Terms & Conditions' block: where it starts and ends."))
    shared: SharedTerms = Field(default_factory=SharedTerms)

    _lenient_lb = _lenient_literal("legal_basis")


class LotBoundary(_Model):
    label: str | None = Field(None, description="The lot's printed number, if any.")
    first_words: str = Field(..., description="First 4–12 words of the lot's text, verbatim.")
    last_words: str = Field(..., description="Last 4–12 words of the lot's text, verbatim.")


class LotBoundaries(_Model):
    """Fallback segmentation: where each lot starts and ends. Code verifies
    every anchor and falls back to a whole read when any fails."""
    lots: list[LotBoundary] = Field(default_factory=list)


class KeyFacts(_Model):
    """The short second read: only the facts a lot is unusable without."""
    lot_label: str | None = None
    reserve_price: Money = Field(default_factory=Money)
    emd: Money = Field(default_factory=Money)
    auction_start: DateFact | None = None
    property_type: KeyFact = Field(default_factory=KeyFact)
    possession: KeyFact = Field(default_factory=KeyFact)
    possession_type: Possession | None = None
    extent_quote: str | None = None
    village: str | None = None
    taluk: str | None = None
    description_first_words: str | None = None

    _lenient_poss = _lenient_literal("possession_type")


class KeyFactsRead(_Model):
    lots: list[KeyFacts] = Field(default_factory=list)


# ── rendering ─────────────────────────────────────────────────────────────────
_CATALOGUE_ORDER = (NoticeRead, SharedTerms, Contact, EmdAccount, LotRead, Party,
                    KeyFact, Money, DateFact, Anchors, Place, Identifier, Extent,
                    Boundary, Schedule, Extra)


def _type_name(annotation) -> str:
    origin = get_origin(annotation)
    if origin is Literal:
        return " | ".join(str(a) for a in get_args(annotation))
    if origin is list:
        return f"list[{_type_name(get_args(annotation)[0])}]"
    args = [a for a in get_args(annotation) if a is not type(None)]
    if args and origin is not None:
        inner = " | ".join(_type_name(a) for a in args)
        return f"{inner} | null" if len(args) < len(get_args(annotation)) else inner
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation.__name__
    return getattr(annotation, "__name__", str(annotation))


def render_catalogue() -> str:
    """The schema as a field catalogue: one line per field with its type and
    description. This is what the prompt carries and what
    pipeline/prompts/extract_enrichment.txt is regenerated from."""
    lines = [f"FIELD CATALOGUE (schema version {SCHEMA_VERSION})", ""]
    for model in _CATALOGUE_ORDER:
        doc = ((model.__doc__ or '').strip().splitlines() or [''])[0]
        lines.append(f"{model.__name__}: {doc}".rstrip(": "))
        for name, f in model.model_fields.items():
            desc = (f.description or "").replace("\n", " ")
            lines.append(f"  - {name} ({_type_name(f.annotation)})" + (f": {desc}" if desc else ""))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def all_field_paths() -> list[str]:
    """'Model.field' for every field, for the prompt-coverage test."""
    return [f"{m.__name__}.{n}" for m in _CATALOGUE_ORDER for n in m.model_fields]


def strict_json_schema(model: type[BaseModel]) -> dict:
    """JSON schema in the shape OpenAI-style strict mode wants: every object
    lists all its properties as required and forbids extras. Optional fields
    keep their ``null`` alternative, so the model can still leave them out."""
    schema = model.model_json_schema()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(schema)
    return schema


if __name__ == "__main__":  # python -m pipeline.reader.schema [--json Model]
    import sys
    if len(sys.argv) > 2 and sys.argv[1] == "--json":
        print(json.dumps(strict_json_schema(globals()[sys.argv[2]]), indent=2))
    else:
        print(render_catalogue())
