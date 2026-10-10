# TamilNilam Geo-Info dropdowns — where the data comes from and how to pull it

**Code:** `scrapers/tamilnilam_dropdowns.py` (District → Taluk → Village → Survey
number) and `scrapers/tamilnilam_subdivisions.py` (→ Sub-division, browser).
**Status:** written 2026-10-10 from a verified third-party trail; **not yet run
live from this repo** (the host resets connections from the cloud box).
**First live run = verification run.** Start with `--level district`.

## The app and its backend

The Android app **TamilNilam Geo-Info** (`org.tnega.tamil.nilam`, published by
TNeGA, "Powered by TNGIS & NIC") and the web **GI Viewer** at
`https://tngis.tn.gov.in/apps/gi_viewer/map-viewer/index.html` are two fronts
on one backend. The search panel's five dropdowns are two different kinds of
call:

| Dropdown | How the viewer fills it | Login? |
| --- | --- | --- |
| District | `GET generic_api/v2/admin_master_district` | not known to be needed |
| Taluk | `GET generic_api/v2/admin_master_taluk` | same |
| Village | `GET generic_api/v2/admin_master_village` | same |
| Survey number | `GET generic_api/v2/admin_master_survey_number` | same |
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
fields seen: district_code, district_english_name, taluk_code, taluk_english_name,
             village_code, village_english_name, survey_number
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
this hierarchy. It runs the listing calls from inside a logged-in page, so
**whether they answer without a session cookie is unconfirmed** — if the first
run gets `success != 1`, paste a logged-in browser's `Cookie` header into
`TAMILNILAM_COOKIE` and retry. The earlier note in
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
