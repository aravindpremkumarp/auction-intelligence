# TamilNilam Geo-Info dropdowns — where the data comes from and how to pull it

**Code:** `scrapers/tamilnilam_dropdowns.py` (District → Taluk → Village → Survey
number) and `scrapers/tamilnilam_subdivisions.py` (→ Sub-division, browser).
**Status:** verified live on 2026-10-10 from a home connection (the cloud box
could not reach the host). The four listing levels answer **without any login
or cookie**, with the default `x-app-name: demo` header:

| Level | Rows | Calls | Time |
| --- | --- | --- | --- |
| district | 38 | 1 | 1 s |
| taluk | 302 | 38 | (in the village run) |
| village | 17,164 | 302 | 340 s for taluk + village |
| survey (Tiruvallur / Ponneri only) | 36,357 across 198 villages | 198 | 226 s |

No 429s, no retries, every row had a code, English name and Tamil name. Each
level also carries an `*_lgd_code` field (`district_lgd_code`,
`taluk_lgd_code`, `village_lgd_code`) that the normaliser leaves under `raw`.
The survey rows are bare `{"survey_number": "..."}`.

**What it did NOT do: improve the village gazetteer.** Running the CSV through
`scripts/refresh_village_gazetteer.py` (dry run) against the live graph:
17,105 of 17,164 villages already present, 6 held under another spelling, 19
missing (17 in Thanjavur / Thiruvonam, 1 each in Tirukalukundram and
Vembakkam), and the graph holds 5,567 villages TNGIS does not. The graph's
thin urban taluks are thin in TNGIS too (Chennai 48, Avadi 21, exactly the
counts the gazetteer script complains about), and Thirumullaivoyal,
Paruthipattu, Selaiyur, Madakulam and Thoraipakkam are absent from TNGIS as
well. The existing gazetteer was evidently loaded from this same register;
closing the resolution gap needs a different source (LGD, eServices) or a
non-revenue-village layer for urban areas, not this scrape.

## The app and its backend

The Android app **TamilNilam Geo-Info** (`org.tnega.tamil.nilam`, published by
TNeGA, "Powered by TNGIS & NIC") and the web **GI Viewer** at
`https://tngis.tn.gov.in/apps/gi_viewer/map-viewer/index.html` are two fronts
on one backend. The search panel's five dropdowns are two different kinds of
call:

| Dropdown | How the viewer fills it | Login? |
| --- | --- | --- |
| District | `GET generic_api/v2/admin_master_district` | **no** (verified 2026-10-10) |
| Taluk | `GET generic_api/v2/admin_master_taluk` | no |
| Village | `GET generic_api/v2/admin_master_village` | no |
| Survey number | `GET generic_api/v2/admin_master_survey_number` | no |
| Sub-division | `POST gi_viewer_api/…/land/check-areg` (encrypted, session-keyed) | **yes** + captcha, 50/hour/account |

Listing calls, exactly as the viewer makes them:

```
BASE = https://tngis.tn.gov.in/apps/generic_api/v2/
headers: x-app-name: demo
         x-requested-with: XMLHttpRequest

GET {BASE}admin_master_district?request_type=district
GET {BASE}admin_master_taluk?district_code=D&request_type=taluk
GET {BASE}admin_master_village?district_code=D&taluk_code=T&request_type=revenue_village
GET {BASE}admin_master_survey_number?district_code=D&taluk_code=T&revenue_village_code=V
        &area_type=rural&data_type=cadastral&request_type=survey_number

reply: {"success": 1, "data": [ ... ]}
fields seen (live, 2026-10-10):
  district: district_code, district_lgd_code, district_english_name, district_tamil_name
  taluk:    taluk_code, taluk_lgd_code, taluk_english_name, taluk_tamil_name
  village:  village_code, village_lgd_code, village_english_name, village_tamil_name
  survey:   survey_number
```

Sub-divisions: the page encrypts `{district_code, taluk_code, village_code,
survey_number, sub_division_number: "jjj", area_type}` with
`landEncryption.encrypt(json, _mapSessionKey)`, POSTs `{payload}` to
`${GI_API_BASE}/land/check-areg` with `X-Session-ID` / `X-CSRF-Token`, and a
`success == 2` reply carries `data[].subdiv_no`. Key, token and cipher are
page-session state, so `tamilnilam_subdivisions.py` logs a real browser in and
runs that same `fetch` inside the page.

Source of the shapes: [sivaramanRW/scraper-glv](https://github.com/sivaramanRW/scraper-glv)
(`scrape.py`), a working Playwright scraper of the viewer that walks exactly
this hierarchy. It runs the listing calls from inside a logged-in page, but the
live run confirmed they answer with no session at all; `TAMILNILAM_COOKIE`
stays as an escape hatch should that change. The earlier note in
`inspiration/2026-07-06-browser-agents-for-tngis-extraction.md` that TNGIS has
"no direct API" was about the map/parcel layer; the listing API is a separate,
plainer thing.

## Running it

```bash
# 1. prove the endpoints answer from your network
python -m scrapers.tamilnilam_dropdowns --level district

# 2. the cheap levels (~38 districts, ~300 taluks, ~17k villages; one call per taluk)
python -m scrapers.tamilnilam_dropdowns --level village --export-csv tn_villages.csv

# 3. survey numbers — one call per village, so scope it
python -m scrapers.tamilnilam_dropdowns --district Tiruvallur --taluk Ponneri --level survey

# 4. sub-divisions — needs a TNGIS citizen account and Playwright
pip install playwright && playwright install chromium
TAMILNILAM_MOBILE=9xxxxxxxxx TAMILNILAM_PASSWORD=... \
python -m scrapers.tamilnilam_dropdowns --district Ariyalur --taluk Andimadam --level subdivision
```

Output lands in `data/tamilnilam/` (git-ignored): `districts.json`,
`taluks.jsonl`, `villages.jsonl`, `survey_numbers.jsonl`,
`subdivisions.jsonl`, `progress.json`. Every row keeps the server's fields
under `raw`. Runs resume: a parent is marked done only after its children are
written; a failed call raises rather than storing an empty list. Pace is
`SOURCE_REQUEST_DELAY_S` (1 s default) for listings and `--subdivision-delay`
(5 s default; the burst limiter trips under ~3 s) for check-areg.

`--export-csv` writes `district,taluk,village,name_ta,tngis_*_code`, which
`scripts/refresh_village_gazetteer.py --from-csv` reads directly (it ignores
the `tngis_*` columns — nobody has confirmed TNGIS's village code is the
within-taluk revenue serial the graph stores, so it is not written as
`village_code`).

## Checking against the Android app itself

If an endpoint drifts, or to learn the app's own `x-app-name`, capture the
app's traffic once:

1. **Static:** download the APK (`org.tnega.tamil.nilam`), open it in
   [jadx](https://github.com/skylot/jadx), search for `generic_api`,
   `admin_master_`, `check-areg`, `x-app-name`. Flutter/React-Native builds
   hide strings in `libapp.so` / a JS bundle — `strings` + grep still works.
2. **Dynamic:** install [HTTP Toolkit](https://httptoolkit.com/) or mitmproxy
   on a rooted emulator / device, add its CA as a system cert, open the app,
   tap through District → Taluk → Village → Survey → Sub-division, and read
   the five requests off the log. Certificate pinning, if present, needs
   Frida's unpinning script.

## Rules of the road

- This is a government service with per-account quotas and a burst detector.
  Keep the defaults, scope survey/sub-division runs to the taluks the auction
  corpus needs, and never run several copies against one account.
- Automated access to TN land portals is "industry-normal but legally gray"
  (see the inspiration note's legal section). Get it reviewed before this
  feeds a product surface; today it is a data-acquisition tool.
- Don't commit `data/tamilnilam/`, `captcha.png`, or any account details.
