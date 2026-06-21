"""LangExtract few-shot examples + prompt for SARFAESI auction-notice extraction.

SOURCE OF TRUTH (option C): the field catalogue is NOT redefined here — it is read
at import time from ``pipeline/prompts/extract_enrichment.txt`` (the canonical
scheme) and wrapped with LangExtract-specific conventions. Edit the scheme in that
one file and this prompt follows automatically; the examples below only have to
keep *demonstrating* the fields.

The three ExampleData objects (single: 736547 / Bank of Baroda; multi: 738029 /
Equitas SFB; apartment: Canara Bank / flat + UDS) are annotated to FULL PARITY
(option A) with the scheme, across these grounded entity classes — chosen so
LangExtract extracts spans (its strength) rather than long attribute lists (its
weakness):

  secured_creditor  borrower  contact  property  full_description  location
  identifier  extent  boundary  schedule  auction_terms  outstanding  emd_account
  full_terms

For MULTI notices every entity of the Nth lot carries ``lot_index=N`` so lots can
be regrouped into one AuctionProperty each. Fields the sample notices do not
contain (e.g. ARC/IBC, carpet area, lat/long, chitta/khata, construction_type)
are still described by the canonical prompt and will be extracted when present —
they are just not demonstrated here.

Run:  python -m pipeline.langextract_examples <path-to-markdown.txt>
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import langextract as lx

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
  title_deed_holder, branch_of_lot, lot_index.
- full_description : the COMPLETE property-description block for ONE lot, copied
  verbatim as a SINGLE span — preamble + all items/schedules + boundaries +
  trailing structural / registration detail, with section labels preserved.
  EXCLUDE terms-of-sale boilerplate (price, EMD, dates, "as is where is"). This
  span deliberately OVERLAPS the granular property/location/extent/boundary spans
  (it is their union) — emit BOTH the block and the granular spans. attrs: lot_index.
- location         : the "Situated At ..." span. attrs: village, taluk, district,
  city, state, panchayat, municipality_corporation, ward_no, hobli,
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

CONVENTIONS:
- extraction_text MUST be copied verbatim from the document (for source grounding).
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
- "Admeasuring ... Northern/Southern/Eastern/Western Side N Feet" gives the
  per-side boundary MEASUREMENT (the dimension) — put N Feet in boundary
  measurement, distinct from adjacency (what abuts that side, e.g. a road/plot).
- "Name of the Title Holder: X" -> property title_deed_holder=X.
- "CERSAI Security Interest Id: N" -> identifier kind=cersai value=N.

The authoritative field semantics and edge cases (DRT "Upset Price", IBC
liquidators, ARC assignor/trust, column-unit money, etc.) are below; follow them:

=== CANONICAL SCHEME (extract_enrichment.txt) ===
"""

PROMPT_DESCRIPTION = _LANGEXTRACT_GUIDE + load_canonical_scheme()


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

EXAMPLES = [SINGLE_EXAMPLE, MULTI_EXAMPLE, APARTMENT_EXAMPLE]


_MODEL_CACHE: dict = {}


def _openrouter_model():
    """Cached OpenAI-compatible model pointed at OpenRouter."""
    from langextract.providers.openai import OpenAILanguageModel
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    model_id = os.environ.get("LANGEXTRACT_MODEL_ID", "google/gemini-2.5-flash")
    if model_id not in _MODEL_CACHE:
        _MODEL_CACHE[model_id] = OpenAILanguageModel(
            model_id=model_id, api_key=key,
            base_url=os.environ.get("OPENROUTER_BASE_URL",
                                    "https://openrouter.ai/api/v1"),
            max_workers=4,
        )
    return _MODEL_CACHE[model_id]


def extract(markdown: str):
    """Run LangExtract over one notice's MinerU markdown.

    Provider is env-driven via LANGEXTRACT_PROVIDER:
      'openrouter' (default) -> OpenRouter, OpenAI-compatible; model
          LANGEXTRACT_MODEL_ID (default 'google/gemini-2.5-flash'), key
          OPENROUTER_API_KEY. Shares the gateway/billing with the rest of the repo.
      'gemini'               -> Gemini API direct; model default 'gemini-2.5-flash',
          key LANGEXTRACT_API_KEY.
    passes (LANGEXTRACT_PASSES, default 2) maximises multi-lot recall; results
    carry char_interval source grounding either way.
    """
    common = dict(
        text_or_documents=markdown, prompt_description=PROMPT_DESCRIPTION,
        examples=EXAMPLES, extraction_passes=int(os.environ.get("LANGEXTRACT_PASSES", "2")),
        max_char_buffer=4000, max_workers=4,
    )
    if os.environ.get("LANGEXTRACT_PROVIDER", "openrouter").lower() == "openrouter":
        return lx.extract(model=_openrouter_model(), fence_output=True,
                          use_schema_constraints=False, **common)
    return lx.extract(model_id=os.environ.get("LANGEXTRACT_MODEL_ID", "gemini-2.5-flash"),
                      api_key=os.environ.get("LANGEXTRACT_API_KEY"), **common)


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
