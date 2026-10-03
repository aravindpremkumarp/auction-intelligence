"""Two worked examples in the reader's own form.

The texts are the two few-shot notices the LangExtract prompt uses (a prose
single-lot notice from Bank of Baroda, a two-row Tata Capital table); the
answers are the same facts expressed as the v2 schema: verbatim quotes,
``not_stated`` where the text does not say (lot 2's property type), a
district left empty where only the registration district is named (no
inference), anchors instead of copied blocks. tests/pipeline/
test_reader_examples_grounded.py checks that every quote and anchor is a
substring of its text, so the examples can never teach the model to invent.

The texts are copied here rather than imported so the reader does not
depend on the ``langextract`` package; pipeline/langextract_examples.py and
its copies go together in PR9.
"""
from __future__ import annotations

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

SINGLE_NOTICE = {
    "legal_basis": "SARFAESI",
    "bank_name_quote": "Bank of Baroda",
    "sale_terms_quote": "As is where is",
    "auction_platform_url": "https://baanknet.com",
    "contacts": [{"quote": "Tel No. 04328 - 225080", "phones": ["04328-225080"]}],
    "shared": {
        "dates": [
            {"status": "found", "quote": "14.05.2026 14.00 to 18.00", "event": "auction_start", "iso": "2026-05-14T14:00"},
            {"status": "found", "quote": "14.05.2026 14.00 to 18.00", "event": "auction_end", "iso": "2026-05-14T18:00"},
            {"status": "found", "quote": "13.05.2026 11.00 to 16.00", "event": "inspection", "iso": "2026-05-13T11:00"},
            {"status": "found", "quote": "DATE : 26.03.2026", "event": "notice_date", "iso": "2026-03-26"},
        ],
        "possession": {"status": "found", "quote": "PhysicalPossession"},
        "possession_type": "physical",
    },
}

SINGLE_SEGMENT = {"lots": [{
    "lot_label": None,
    "borrowers": [
        {"name_quote": "M/s Health Mushrooms", "role": "borrower",
         "address_quote": "D.No.519/4, Keelakkarai, Perambalur - 621 219"},
        {"name_quote": "Mrs Suganthi Johnpeter (Proprietor)", "role": "proprietor",
         "address_quote": "No 6, Indira Nagar, Elambalur"},
        {"name_quote": "Mr Johnpeter Sebastian (Guarantor)", "role": "guarantor",
         "address_quote": "No 4, Indira Nagar, Elambalur"},
    ],
    "property_type": {"status": "found", "quote": "vacant land"},
    "property_type_norm": "land",
    "possession": {"status": "found", "quote": "PhysicalPossession"},
    "possession_type": "physical",
    "reserve_price": {"status": "found", "quote": "Rs.9,50,000/-", "unit": "rupees"},
    "emd": {"status": "found", "quote": "Rs.95,000/-", "unit": "rupees"},
    "bid_increment": {"status": "found", "quote": "Rs.25,000/-", "unit": "rupees"},
    "dates": [
        {"status": "found", "quote": "Dues as on 26.03.2026", "event": "outstanding_as_on", "iso": "2026-03-26"},
    ],
    "outstanding": {"status": "found", "quote": "Rs 53,91,240.72", "unit": "rupees"},
    "location": {"quote": "Perambalur North Village, Perambalur Taluk and District",
                 "village": "Perambalur North", "taluk": "Perambalur", "district": "Perambalur"},
    "identifiers": [
        {"kind": "survey_new", "value": "256/1F", "quote": "UDR SF No 256/1F"},
        {"kind": "survey_old", "value": "390/1", "quote": "SF No 390/1"},
        {"kind": "plot", "value": "4", "quote": "Plot No 4"},
    ],
    "extents": [{"role": "total_area", "quote": "1242 ¼ Square feet (115.43 Square Meters)"}],
    "boundaries": [
        {"side": "east", "adjacency_quote": "Plot No 5", "measurement_quote": "30 feet"},
        {"side": "west", "adjacency_quote": "Plot No 1 belonged to Kowsalya and Varatharajan",
         "measurement_quote": "28 ¼ feet"},
        {"side": "south", "adjacency_quote": "Plot belongs to Gomathi W/o Vijayakumar"},
        {"side": "north", "adjacency_quote": "2nd item"},
    ],
    "schedules": [
        {"label": "Item 1", "type": "land", "extent_quote": "1023 ¼ Square feet (95.11 Square meters)",
         "anchors": {"first_words": "Item No 1 : An extent of East West",
                     "last_words": "1023 ¼ Square feet (95.11 Square meters)"}},
        {"label": "Item 2", "type": "land", "extent_quote": "218 ¼ square feet (20.32 Square meters)",
         "anchors": {"first_words": "Item No 2 : An extent of",
                     "last_words": "218 ¼ square feet (20.32 Square meters)."}},
    ],
    "description": {"first_words": "Equitable mortgage of vacant land located in",
                    "last_words": "1242 ¼ Square feet (115.43 Square Meters)."},
}]}

SERIAL_ROWS_TEXT = (
    "# TATA CAPITAL HOUSING FINANCE LIMITED\n\n"
    "(Under Rule 8(6) read with Rule 9(1) of the Security Interest (Enforcement) "
    "Rules 2002)\n\n"
    "<table><tr><td>Sr. No</td><td>LoanA/c. No</td><td>Name of Borrower(s) "
    "/Co-borrower(s)</td><td>Amountas per DemandNotice</td><td>Reserve Price</td>"
    "<td>Outstanding as on</td></tr><tr><td>1</td><td>TCHHF0806000100229003</td>"
    "<td>MR. GOKULNATH.JMRS.ANANTHI SELVARAJ,</td><td>Rs. 19,48,722/-&amp;"
    "05-02-2026</td><td>Rs.26,83,000/-Earnest Money Deposit (EMD): - "
    "Rs.2,68,300/-Type of possession: - Physical</td><td>Rs. 2072586/-&amp;"
    "25-06-2026</td></tr></table>\n\n"
    "Description of the Immovable Property: All that piece and parcel of the "
    "Erode District, Erode RD, Surampatti SRO, Modakurichi Taluk, punjai "
    "kalamangalam village, resurvey no.12/4A1B, patta No.2089, House site no.14 "
    "for an extent of 1987.50 sq.feet house site, within the following "
    "boundaries:- House site No's 10,11 on the north, 10.0 meter breadth "
    "east-west road on the south, House site no.15 on the west, House site "
    "no.13, other lands on the east.\n\n"
    "<table><tr><td rowspan=\"3\">2</td><td rowspan=\"3\">TCHHL0991000100279718"
    "</td><td rowspan=\"3\">MRS. REGINA R. THIRUNAVUKARASU</td><td rowspan=\"3\">"
    "Rs. 9,68,756/- &amp; 05-02-2025</td><td>Rs.9,85,000/-</td><td rowspan=\"3\">"
    "Rs. 1291781/- &amp; 25-06-2026</td></tr><tr><td>Earnest Money Deposit "
    "(EMD): - Rs.98,500/-</td></tr><tr><td>Type of possession: - Physical</td>"
    "</tr></table>\n\n"
    "Description of the Immovable Property: All that piece and parcel of the "
    "New Natham S.No.281/26, Door.No.3/50, Total Extent 1065 Sq.Ft., "
    "Vadivilliputhiryenthal village, Manamadurai Taluk, Virudhunagar Regd.Dist, "
    "Veerachozhan SRO. Boundaries: North by- Natham S.No.281/28 Santhu, South by- "
    "S.No.281/25 Muthuramu wife Sagunthala vacant land, East by- Natham "
    "S.No.281/27 Natham Road, West by- S.No.281/24 Sundaram vacant land.\n\n"
    "The E-auction of the properties will take place through portal "
    "https://auctionbazaar.com on 11-08-2026 between 2.00 PM to 3.00 PM."
)

SERIAL_ROWS_NOTICE = {
    "legal_basis": "SARFAESI",
    "bank_name_quote": "TATA CAPITAL HOUSING FINANCE LIMITED",
    "auction_platform_url": "https://auctionbazaar.com",
    "shared": {"dates": [
        {"status": "found", "quote": "11-08-2026 between 2.00 PM to 3.00 PM", "event": "auction_start", "iso": "2026-08-11T14:00"},
        {"status": "found", "quote": "11-08-2026 between 2.00 PM to 3.00 PM", "event": "auction_end", "iso": "2026-08-11T15:00"},
    ]},
}

SERIAL_ROWS_SEGMENT = {"lots": [
    {
        "lot_label": "1",
        "borrowers": [{"name_quote": "MR. GOKULNATH.J", "role": "borrower"},
                      {"name_quote": "MRS.ANANTHI SELVARAJ", "role": "co-borrower"}],
        "property_type": {"status": "found", "quote": "house site"},
        "property_type_norm": "plot",
        "possession": {"status": "found", "quote": "Type of possession: - Physical"},
        "possession_type": "physical",
        "reserve_price": {"status": "found", "quote": "Rs.26,83,000/-", "unit": "rupees"},
        "emd": {"status": "found", "quote": "Rs.2,68,300/-", "unit": "rupees"},
        "dates": [{"status": "found", "quote": "25-06-2026", "event": "outstanding_as_on", "iso": "2026-06-25"}],
        "outstanding": {"status": "found", "quote": "Rs. 2072586/-", "unit": "rupees"},
        "loan_account_nos": ["TCHHF0806000100229003"],
        "location": {"quote": "Erode District, Erode RD, Surampatti SRO, Modakurichi Taluk, punjai kalamangalam village",
                     "village": "punjai kalamangalam", "taluk": "Modakurichi", "district": "Erode",
                     "registration_district": "Erode", "registration_sub_district": "Surampatti"},
        "identifiers": [
            {"kind": "survey_new", "value": "12/4A1B", "quote": "resurvey no.12/4A1B"},
            {"kind": "patta", "value": "2089", "quote": "patta No.2089"},
            {"kind": "plot", "value": "14", "quote": "House site no.14"},
        ],
        "extents": [{"role": "total_area", "quote": "1987.50 sq.feet"}],
        "boundaries": [
            {"side": "north", "adjacency_quote": "House site No's 10,11"},
            {"side": "south", "adjacency_quote": "10.0 meter breadth east-west road"},
            {"side": "west", "adjacency_quote": "House site no.15"},
            {"side": "east", "adjacency_quote": "House site no.13, other lands"},
        ],
        "description": {"first_words": "All that piece and parcel of the Erode District",
                        "last_words": "other lands on the east."},
    },
    {
        "lot_label": "2",
        "borrowers": [{"name_quote": "MRS. REGINA R. THIRUNAVUKARASU", "role": "borrower"}],
        "property_type": {"status": "not_stated"},
        "possession": {"status": "found", "quote": "Type of possession: - Physical"},
        "possession_type": "physical",
        "reserve_price": {"status": "found", "quote": "Rs.9,85,000/-", "unit": "rupees"},
        "emd": {"status": "found", "quote": "Rs.98,500/-", "unit": "rupees"},
        "dates": [{"status": "found", "quote": "25-06-2026", "event": "outstanding_as_on", "iso": "2026-06-25"}],
        "outstanding": {"status": "found", "quote": "Rs. 1291781/-", "unit": "rupees"},
        "loan_account_nos": ["TCHHL0991000100279718"],
        "location": {"quote": "Vadivilliputhiryenthal village, Manamadurai Taluk, Virudhunagar Regd.Dist, Veerachozhan SRO",
                     "village": "Vadivilliputhiryenthal", "taluk": "Manamadurai", "district": None,
                     "registration_district": "Virudhunagar", "registration_sub_district": "Veerachozhan"},
        "identifiers": [
            {"kind": "survey_new", "value": "281/26", "quote": "New Natham S.No.281/26"},
            {"kind": "door_new", "value": "3/50", "quote": "Door.No.3/50"},
        ],
        "extents": [{"role": "total_area", "quote": "Total Extent 1065 Sq.Ft."}],
        "boundaries": [
            {"side": "north", "adjacency_quote": "Natham S.No.281/28 Santhu"},
            {"side": "south", "adjacency_quote": "S.No.281/25 Muthuramu wife Sagunthala vacant land"},
            {"side": "east", "adjacency_quote": "Natham S.No.281/27 Natham Road"},
            {"side": "west", "adjacency_quote": "S.No.281/24 Sundaram vacant land"},
        ],
        "description": {"first_words": "All that piece and parcel of the New Natham",
                        "last_words": "Sundaram vacant land."},
    },
]}

#: (text, SegmentRead dict, NoticeRead dict)
EXAMPLES = [
    (SINGLE_TEXT, SINGLE_SEGMENT, SINGLE_NOTICE),
    (SERIAL_ROWS_TEXT, SERIAL_ROWS_SEGMENT, SERIAL_ROWS_NOTICE),
]
