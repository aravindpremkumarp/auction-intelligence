"""LangExtract few-shot examples + prompt for SARFAESI auction-notice extraction.

These two ExampleData objects are the heart of a LangExtract run. Each
`extraction_text` is **verbatim** from a real notice in our corpus (single:
auction_id 736547 / Bank of Baroda; multi: 738029 / Equitas SFB) so LangExtract's
character-level source grounding aligns to the source. Structured/normalised
values live in `attributes`; the verbatim span is only the anchor.

Entity classes are chosen to map cleanly onto the Neo4j graph:
  secured_creditor -> (:Bank)/(:Branch)      borrower    -> (:Borrower)
  property         -> (:AuctionProperty)     schedule    -> sub-parcel of a lot
  auction_terms    -> price/date props        outstanding -> dues props
  emd_account      -> EMD remittance props
For MULTI notices each lot's entities share a `lot_index` attribute so they can
be regrouped into one AuctionProperty per lot.

Full field semantics live in pipeline/prompts/extract_enrichment.txt; this file
is the LangExtract-shaped, example-driven encoding of that scheme.

Run:  python -m pipeline.langextract_examples <path-to-markdown.txt>
"""
from __future__ import annotations

import os
import sys

import langextract as lx

# --------------------------------------------------------------------------- #
# Prompt — condensed from extract_enrichment.txt. LangExtract is example-driven,
# so keep this short; the examples below carry the precise field semantics.
# --------------------------------------------------------------------------- #
PROMPT_DESCRIPTION = """\
Extract structured data from an Indian bank auction sale notice (SARFAESI / DRT /
IBC). Use these entity classes:
- secured_creditor: the selling bank/ARC/NBFC (attrs: branch, legal_basis,
  assignor_bank, trust_name).
- borrower: each borrower/guarantor (attrs: role, address).
- property: each auction lot's immovable property (attrs: property_type,
  possession_type, survey_numbers_old/new, patta_no, plot_no, total_area,
  extent_sqft, village, taluk, district, registration_district,
  boundary_north/south/east/west, lot_index).
- schedule: a sub-parcel (Item/Schedule A,B..) of one lot (attrs: label,
  extent, lot_index).
- auction_terms: price & dates for a lot (attrs: reserve_price_num, emd_num,
  bid_increment_num, auction_dt, inspection_dt, lot_index).
- outstanding: the dues for a lot (attrs: amount_num, as_on, loan_account_no,
  lot_index).
- emd_account: where EMD is remitted (attrs: account_name, account_no, ifsc).
Rules: extraction_text MUST be copied verbatim from the document. Put rupee
amounts as integers in attributes (Rs.9,50,000 -> 950000). Preserve the unicode
fractions ½ ¼ ¾. Dates as ISO 8601 (YYYY-MM-DDThh:mm) — always include the time
when the notice gives one. For multi-lot notices, tag every entity of the Nth lot
with lot_index=N.
COMPLETENESS: for EVERY `property`, always populate these attributes whenever the
text contains them — property_type, survey_numbers_old, survey_numbers_new,
patta_no, total_area, extent_sqft, village, taluk, district,
registration_district, and boundary_north/south/east/west. Do NOT stop at
property_type. OMIT any attribute that is genuinely absent — NEVER output the
literal string "null", "NA", or an empty value; just leave the key out.
"""

# --------------------------------------------------------------------------- #
# Example 1 — SINGLE notice (one lot, two sub-items). Source: auction_id 736547.
# --------------------------------------------------------------------------- #
SINGLE_TEXT = (
    "E-Auction Sale Notice ... possession of which has been taken by the "
    "Authorised Officer of Bank of Baroda, Secured Creditor, will be sold on "
    "\"As is where is\". "
    "M/s Health Mushrooms D.No.519/4, Keelakkarai, Perambalur - 621 219. "
    "1. Mrs Suganthi Johnpeter (Proprietor) No 6, Indira Nagar, Elambalur. "
    "2. Mr Johnpeter Sebastian (Guarantor) No 4, Indira Nagar, Elambalur. "
    "Equitable mortgage of vacant land located in UDR SF No 256/1F, SF No 390/1, "
    "Plot No 4 Northern Side & Western Side, Perambalur North Village, Perambalur "
    "Taluk and District. "
    "Item No 1 : An extent of East West 30 feet on both sides, North South Eastern "
    "site 40 feet, Western side 28 ¼ feet, admeasuring an extent of 1023 ¼ Square "
    "feet (95.11 Square meters) vacant site Northern side of Plot No 4 having the "
    "following four boundaries : East of Plot No 5, West of Plot No 1 belonged to "
    "Kowsalya and Varatharajan, South of Plot belongs to Gomathi W/o Vijayakumar, "
    "North of 2nd item. "
    "Item No 2 : An extent of East west 5 feet on both sides, North South 43 ¼ feet "
    "on both sides admeasuring an extent of 218 ¼ square feet (20.32 Square meters). "
    "The total extent of above two items of plots are 1242 ¼ Square feet vacant site "
    "(115.43 Square Meters). "
    "Dues as on 26.03.2026 Cumulative Total Dues of Rs 53,91,240.72. "
    "Date & Time of E-auction 14.05.2026 14.00 to 18.00. "
    "1.Rs.9,50,000/- 2.Rs.95,000/- 3.Rs.25,000/-. PhysicalPossession. "
    "Property Inspection date & Time 13.05.2026 11.00 to 16.00. "
    "prospective bidders may contact the Authorised officer on Tel No. 04328 - 225080. "
    "DATE : 26.03.2026 PLACE : PERAMBALUR"
)

SINGLE_EXAMPLE = lx.data.ExampleData(
    text=SINGLE_TEXT,
    extractions=[
        lx.data.Extraction(
            extraction_class="secured_creditor",
            extraction_text="Bank of Baroda",
            attributes={"legal_basis": "SARFAESI", "branch": "Perambalur"},
        ),
        lx.data.Extraction(
            extraction_class="borrower",
            extraction_text="M/s Health Mushrooms",
            attributes={"role": "borrower"},
        ),
        lx.data.Extraction(
            extraction_class="borrower",
            extraction_text="Mrs Suganthi Johnpeter (Proprietor)",
            attributes={"role": "proprietor",
                        "address": "No 6, Indira Nagar, Elambalur"},
        ),
        lx.data.Extraction(
            extraction_class="borrower",
            extraction_text="Mr Johnpeter Sebastian (Guarantor)",
            attributes={"role": "guarantor",
                        "address": "No 4, Indira Nagar, Elambalur"},
        ),
        lx.data.Extraction(
            extraction_class="property",
            extraction_text=("Equitable mortgage of vacant land located in UDR SF "
                             "No 256/1F, SF No 390/1, Plot No 4 Northern Side & "
                             "Western Side, Perambalur North Village, Perambalur "
                             "Taluk and District."),
            attributes={
                "lot_index": "1",
                "property_type": "vacant land",
                "possession_type": "physical",
                "survey_numbers_new": "UDR SF 256/1F",
                "survey_numbers_old": "SF 390/1",
                "plot_no": "4",
                "village": "Perambalur North",
                "taluk": "Perambalur",
                "district": "Perambalur",
                "total_area": "1242 ¼ sq.ft (115.43 sq.m)",
                "extent_sqft": "1242.25",
            },
        ),
        lx.data.Extraction(
            extraction_class="schedule",
            extraction_text=("Item No 1 : An extent of East West 30 feet on both "
                             "sides, North South Eastern site 40 feet, Western side "
                             "28 ¼ feet, admeasuring an extent of 1023 ¼ Square feet "
                             "(95.11 Square meters)"),
            attributes={"lot_index": "1", "label": "Item 1",
                        "extent": "1023 ¼ sq.ft (95.11 sq.m)",
                        "boundary_east": "Plot No 5",
                        "boundary_west": "Plot No 1 (Kowsalya & Varatharajan)",
                        "boundary_south": "Plot of Gomathi W/o Vijayakumar",
                        "boundary_north": "2nd item"},
        ),
        lx.data.Extraction(
            extraction_class="schedule",
            extraction_text=("Item No 2 : An extent of East west 5 feet on both "
                             "sides, North South 43 ¼ feet on both sides admeasuring "
                             "an extent of 218 ¼ square feet (20.32 Square meters)"),
            attributes={"lot_index": "1", "label": "Item 2",
                        "extent": "218 ¼ sq.ft (20.32 sq.m)"},
        ),
        lx.data.Extraction(
            extraction_class="auction_terms",
            extraction_text="1.Rs.9,50,000/- 2.Rs.95,000/- 3.Rs.25,000/-",
            attributes={"lot_index": "1", "reserve_price_num": "950000",
                        "emd_num": "95000", "bid_increment_num": "25000",
                        "auction_dt": "2026-05-14T14:00",
                        "inspection_dt": "2026-05-13T11:00"},
        ),
        lx.data.Extraction(
            extraction_class="outstanding",
            extraction_text="Cumulative Total Dues of Rs 53,91,240.72",
            attributes={"lot_index": "1", "amount_num": "5391240.72",
                        "as_on": "2026-03-26"},
        ),
    ],
)

# --------------------------------------------------------------------------- #
# Example 2 — MULTI notice (many lots; two shown). Source: auction_id 738029.
# --------------------------------------------------------------------------- #
MULTI_TEXT = (
    "E-Auction Sale Notice ... possession of which has been taken by the "
    "Authorized Officer of Equitas small finance Bank, Secured Creditor. "
    "1. Ponniyammal M 2. Munusamy A (Both are residing at Gummidipoondi, Chennai "
    "Region, Tamil Nadu, 601201). "
    "All That Piece And Parcel Of Land And Building, Comprised In S.Nos.108/6A, "
    "99/13, As Per Patta No.96, New S.No.99/13A, & 108/6A, With An Extent Of 1305 "
    "Sq.Ft., Situated At Penia Chozhiyampakkam Village, Gummidipoondi Taluk, "
    "Thiruvallur District. "
    "Rs.10,68,000/- Rs.1,06,800/- Rs.10,000/-. 11.05.2026 From 11.00 AM to 12.30 PM. "
    "Loan Account No:-700006541659 (Total Outstanding being Rs.8,12,695/- as on "
    "24.03.2026). "
    "Mr/Mrs. Indhra D Mr/Mrs. V Ananthi (residing at No.44, Lakshmi koiil Street, "
    "Gummidipoodi, Tamil Nadu, 601201). "
    "All That Piece And Parcel Of Land And Building, Comprised In S.Nos.41/28, 25/4, "
    "With An Extent Of 1526 Sq.Ft., Situated At Chinna Chozhiyambampakkam Village, "
    "Gummidipoodi Taluk, Thiruvallur District And Bounded On: (North By)- Pathway "
    "(South By)- Land Belongs To Mr.Govindhan (East By)- Land Belongs To Mr.Murugan "
    "(West By)- Land Belongs To Mr.Aadhiappan Reddy. "
    "Rs.7,73,000/-. 11.05.2026 From 11.00 AM to 12.30 PM. Loan Account No:- "
    "700009454343 (Total Outstanding being Rs.6,84,413/- as on 24.03.2026). "
    "The intending purchaser is required to submit EMD by way of NEFT/RTGS/DD in the "
    "account of \"Equitas Small Finance Bank Ltd\" Account No- 200000807725 and IFSC "
    "code- ESFB0001001 on or before date: 08.05.2026"
)

MULTI_EXAMPLE = lx.data.ExampleData(
    text=MULTI_TEXT,
    extractions=[
        lx.data.Extraction(
            extraction_class="secured_creditor",
            extraction_text="Equitas small finance Bank",
            attributes={"legal_basis": "SARFAESI",
                        "normalized_name": "Equitas Small Finance Bank"},
        ),
        # ---- Lot 1 ----
        lx.data.Extraction(
            extraction_class="borrower",
            extraction_text="Ponniyammal M",
            attributes={"lot_index": "1", "role": "borrower",
                        "address": "Gummidipoondi, Chennai Region, Tamil Nadu, 601201"},
        ),
        lx.data.Extraction(
            extraction_class="borrower",
            extraction_text="Munusamy A",
            attributes={"lot_index": "1", "role": "co-borrower"},
        ),
        lx.data.Extraction(
            extraction_class="property",
            extraction_text=("All That Piece And Parcel Of Land And Building, "
                             "Comprised In S.Nos.108/6A, 99/13, As Per Patta No.96, "
                             "New S.No.99/13A, & 108/6A, With An Extent Of 1305 "
                             "Sq.Ft., Situated At Penia Chozhiyampakkam Village, "
                             "Gummidipoondi Taluk, Thiruvallur District."),
            attributes={"lot_index": "1", "property_type": "land and building",
                        "patta_no": "96",
                        "survey_numbers_old": "108/6A, 99/13",
                        "survey_numbers_new": "99/13A, 108/6A",
                        "total_area": "1305 sq.ft", "extent_sqft": "1305",
                        "village": "Penia Chozhiyampakkam",
                        "taluk": "Gummidipoondi", "district": "Thiruvallur"},
        ),
        lx.data.Extraction(
            extraction_class="auction_terms",
            extraction_text="Rs.10,68,000/- Rs.1,06,800/- Rs.10,000/-",
            attributes={"lot_index": "1", "reserve_price_num": "1068000",
                        "emd_num": "106800", "bid_increment_num": "10000",
                        "auction_dt": "2026-05-11T11:00"},
        ),
        lx.data.Extraction(
            extraction_class="outstanding",
            extraction_text="Total Outstanding being Rs.8,12,695/- as on 24.03.2026",
            attributes={"lot_index": "1", "amount_num": "812695",
                        "as_on": "2026-03-24", "loan_account_no": "700006541659"},
        ),
        # ---- Lot 2 ----
        lx.data.Extraction(
            extraction_class="borrower",
            extraction_text="Indhra D",
            attributes={"lot_index": "2", "role": "borrower",
                        "address": "No.44, Lakshmi koiil Street, Gummidipoodi"},
        ),
        lx.data.Extraction(
            extraction_class="property",
            extraction_text=("All That Piece And Parcel Of Land And Building, "
                             "Comprised In S.Nos.41/28, 25/4, With An Extent Of 1526 "
                             "Sq.Ft., Situated At Chinna Chozhiyambampakkam Village, "
                             "Gummidipoodi Taluk, Thiruvallur District And Bounded "
                             "On: (North By)- Pathway (South By)- Land Belongs To "
                             "Mr.Govindhan (East By)- Land Belongs To Mr.Murugan "
                             "(West By)- Land Belongs To Mr.Aadhiappan Reddy"),
            attributes={"lot_index": "2", "property_type": "land and building",
                        "survey_numbers_old": "41/28, 25/4",
                        "total_area": "1526 sq.ft", "extent_sqft": "1526",
                        "village": "Chinna Chozhiyambampakkam",
                        "taluk": "Gummidipoodi", "district": "Thiruvallur",
                        "boundary_north": "Pathway",
                        "boundary_south": "Land of Mr.Govindhan",
                        "boundary_east": "Land of Mr.Murugan",
                        "boundary_west": "Land of Mr.Aadhiappan Reddy"},
        ),
        lx.data.Extraction(
            extraction_class="auction_terms",
            extraction_text="Rs.7,73,000/-",
            attributes={"lot_index": "2", "reserve_price_num": "773000",
                        "auction_dt": "2026-05-11T11:00"},
        ),
        lx.data.Extraction(
            extraction_class="outstanding",
            extraction_text="Total Outstanding being Rs.6,84,413/- as on 24.03.2026",
            attributes={"lot_index": "2", "amount_num": "684413",
                        "as_on": "2026-03-24", "loan_account_no": "700009454343"},
        ),
        # ---- notice-level EMD account ----
        lx.data.Extraction(
            extraction_class="emd_account",
            extraction_text=("Account No- 200000807725 and IFSC code- ESFB0001001"),
            attributes={"account_name": "Equitas Small Finance Bank Ltd",
                        "account_no": "200000807725", "ifsc": "ESFB0001001"},
        ),
    ],
)

EXAMPLES = [SINGLE_EXAMPLE, MULTI_EXAMPLE]


def extract(markdown: str):
    """Run LangExtract over one notice's MinerU markdown.

    Model/key are env-driven so this works with Gemini API, Vertex, or a local
    Ollama model (set LANGEXTRACT_MODEL_ID=ollama/gemma3:4b and a dummy key).
    extraction_passes=3 maximises multi-lot recall; results carry char_interval
    source grounding for the review UI.
    """
    return lx.extract(
        text_or_documents=markdown,
        prompt_description=PROMPT_DESCRIPTION,
        examples=EXAMPLES,
        model_id=os.environ.get("LANGEXTRACT_MODEL_ID", "gemini-2.5-flash"),
        api_key=os.environ.get("LANGEXTRACT_API_KEY"),
        extraction_passes=3,
        max_char_buffer=2000,
        max_workers=10,
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    md = open(sys.argv[1], encoding="utf-8").read()
    result = extract(md)
    for e in result.extractions:
        loc = getattr(e, "char_interval", None)
        print(f"[{e.extraction_class}] {e.extraction_text[:70]!r} "
              f"@ {loc}  attrs={e.attributes}")
    # Optional: lx.visualize(result) -> interactive HTML for the review queue.
