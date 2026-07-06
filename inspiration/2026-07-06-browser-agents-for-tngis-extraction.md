# Browser-agent tools for extracting land data from Tamil Nilam (TNGIS)

- **Status:** exploring  <!-- captured | exploring | planned | implemented | parked -->
- **Date added:** 2026-07-06
- **Source:**
  - Browser-BC / "Journey Forge Local" — https://github.com/Einsia/Browser-BC
  - Alibaba page-agent — https://github.com/alibaba/page-agent
  - Target site — https://tngis.tn.gov.in/apps/gi_viewer/index.html
  - End-goal reference — https://verified.realestate/landlens/demo/legal-opinion
- **Tags:** scraping, browser-automation, gis, tamil-nadu, data-extraction,
  ai-agent, legal-opinion, due-diligence, dossier
- **See also:** the "Update 2026-07-06" section at the bottom is the load-bearing
  part — the browser agents are one layer of a much bigger legal-opinion goal.

## What it is

Two open-source browser-agent tools we want to point at the **Tamil Nilam GI
Viewer** (TNGIS) to pull land/parcel information.

**1. Browser-BC (Journey Forge Local)** — records free-form browser tasks and
distills them into reusable **Claude skills** (`SKILL.md`). Pipeline is
`atomize → classify → bucket per domain+capability → distill → SKILL.md`.
Python runtime + a Chrome MV3 recording extension; can *execute* the learned
task via a Playwright MCP integration. So you demonstrate an extraction once,
and it becomes a repeatable skill.

**2. Alibaba page-agent** — "the GUI agent living in your webpage." One script
tag gives any page a natural-language AI agent that drives the DOM as **text**
(no screenshots / multimodal needed), so `agent.execute('search survey number
123/4 in Coimbatore and open the parcel')` works. Client-side, supports
mainstream + local LLMs, has an optional Chrome extension for multi-page flows
and an MCP server (beta). Strong at form-filling and multi-step admin/ERP-style
UIs — which is basically what a GIS viewer's search panel is.

## The target: Tamil Nilam GI Viewer

- Government GIS platform for Tamil Nadu — "real-time access to land parcel
  data across Tamil Nadu."
- Shows **land parcels** with boundaries, **survey numbers + Patta (ownership)
  details**, **FMB** (field measurement) data, and layered basemaps.
- JavaScript single-page app (map framework not confirmed from the shell — most
  likely ArcGIS / OpenLayers / Leaflet). Search is by survey number / patta.
- **Requires registration + login**, with separate citizen vs. official
  pathways.

## What caught my eye

For auction due diligence we constantly need to confirm the *real* land
identity behind a listing — survey number, patta/ownership, and parcel
boundaries. Tamil Nilam is the authoritative source for that in TN. A
browser-agent that logs in, searches a survey number, and reads back the parcel
+ ownership record would let us enrich auction properties with ground-truth
land data instead of trusting the listing.

## How I plan to integrate it

**Where it fits:** a new enrichment step in `scrapers/` + `pipeline/` — given an
auction property's survey number / district / taluk, fetch the corresponding
Tamil Nilam parcel + patta record and attach it to the property before scoring.

**Two candidate approaches (test both):**

1. **GUI automation (page-agent or Browser-BC skill).** Drive the actual
   viewer: log in, type the survey number, open the parcel, read the info
   panel. Robust to a JS-heavy UI and the login wall. page-agent's text-DOM
   approach is cheap (no vision model); Browser-BC would let us *record the flow
   once* and replay it as a Claude skill via Playwright MCP.
2. **Hit the underlying map services directly.** A GIS SPA almost always talks
   to ArcGIS REST / WFS / GeoServer endpoints under the hood. If we can capture
   those network calls (via the agent's first run, or DevTools), querying them
   directly by survey number would be far faster and more stable than clicking.
   **Likely the better long-term path** — use the GUI agent to *discover* the
   endpoints, then call them directly.

**What we'd do differently:** wrap whichever path we pick behind a normal
scraper interface with caching + rate limiting, so the rest of the pipeline
doesn't care that a browser agent is involved.

## Open questions / unknowns

- **Terms of use & legality.** It's a government portal behind a login — need to
  confirm automated access is permitted, whether an official vs. citizen account
  is required, and respect rate limits. Flag before building anything.
- **Auth.** Login is required — where do credentials live (`.env`), and does the
  session hold long enough to batch lookups?
- **Which map framework / endpoints** back the viewer — determines whether
  approach #2 is even possible.
- **Coverage / matching** — can we reliably map an auction listing to a survey
  number + district/taluk to query with?
- **Cost** — page-agent per-action LLM calls across thousands of properties vs. a
  one-time endpoint-discovery + direct calls.

## Assets

- `./assets/` — (add a screenshot of the viewer's search panel + a parcel info
  panel when we start; also save the captured network requests / endpoint list
  here once discovered).

## Next step

Decide **buy-vs-build** for the data-acquisition layer (Landeed API vs. our own
CAPTCHA-gated browser scrapers) before writing any scraper — see the update
below. Then promote the dossier legal-opinion work to a `docs/design/` spec.

---

## Update 2026-07-06 — the real end goal + verified feasibility

After discussion, the actual end goal is bigger than "scrape TNGIS." It is to
**auto-generate a per-property legal-opinion / due-diligence report** for any TN
property, in the shape of https://verified.realestate/landlens/demo/legal-opinion
(LandLens): title chain, patta/A-Register cross-check, encumbrance analysis,
CERSAI + eCourts search, GIS zone screening, a 0–100 risk score with verdict,
severity-ranked issues, and recommendations. TNGIS is just **one of ~6 data
feeds**, not the product.

### Key reframe: this is already our dossier Phase 2

`docs/design/2026-06-13-document-dossier-ai-analysis.md` is explicitly
"locker-first, verdicts-later" and its named **Phase 2 is the AI legal opinion**.
We already have the hard-won foundations:

- The **join keys are already extracted** by the MinerU + LangExtract pipeline
  (survey numbers with old/new kind, patta/chitta/khata numbers, CERSAI IDs,
  sale-deed numbers, boundaries, encumbrance disclosures, DRT/IBC court refs,
  village/taluk/district).
- **Dossier v1 is shipped dark** (`DOSSIERS_ENABLED=false`) — 9-category /
  ~50-doc-type TN diligence taxonomy + a 0–100 Diligence Readiness Score.
- `pipeline/validators.py` already does **"100 − severity penalties" over a
  typed `{code, severity, msg}` issues list** — structurally identical to
  LandLens's severity-ranked findings + verdict, just aimed at extraction
  quality today.
- The **deep-research chat mode** already emits a 7-section due-diligence report
  (ephemeral, in-chat).

**What's missing is not the AI/scoring/report — it's (1) the data-acquisition
layer (zero gov connectors exist) and (2) the reconciliation engine** (title-chain
linking, EC-vs-notice lien match, and the A-Register-"Poramboke"-vs-Patta-"Punjai"
classification mismatch that was the single *Critical* finding in the LandLens
demo). Prereq: promote survey/patta numbers to first-class normalized fields
(a `SurveyNumber` node was removed 2026-05); parse `72/1B` subdivisions + old↔new.

### Verified data-source feasibility (none have a public API)

Every TN source is a human-facing web form — automation means driving a headless
browser through a CAPTCHA, not calling an API.

| Source | Gate | Verdict |
| --- | --- | --- |
| Guideline value (TNREGINET) | CAPTCHA, no login | 🟢 easiest |
| Encumbrance Certificate (TNREGINET) | CAPTCHA, view free, no login | 🟢 scriptable |
| Patta/Chitta/A-Register/FMB (eservices.tn.gov.in) | CAPTCHA; Tamil Nilam is OTP-walled | 🟡 browser-agent |
| CERSAI mortgage search | CAPTCHA + ₹10/search fee | 🟡 scriptable, paid per query |
| eCourts litigation | CAPTCHA, no official API | 🟠 hard DIY / buy reseller API |
| **TNGIS gi_viewer** | **TN-SSO login wall, sealed** | 🔴 manual-only |

**Correction on TNGIS specifically:** it does **not** expose reachable ArcGIS/
GeoServer endpoints. The viewer fronts TN Single Sign-On; map data flows through
a token-gated custom API (`/apps/gi_viewer_api/gi_mvc/...`); `/arcgis/rest/services`
and `/geoserver/wms` return **404 unauthenticated**. There is **no bypass to the
underlying API** — the earlier "GUI vs. direct API" question is settled as *login
wall, no direct API*. Login-free GIS fallback = national ISRO **Bhuvan / NIC**
layers (`bhuvan-vec1.nrsc.gov.in/bhuvan/wms`), but those are not TN parcel-level
cadastre.

### Buy-vs-build shortcut

**Landeed Document Procurement API** (already catalogued in
`docs/landeed_tn_records.md`) is survey-no/village-keyed, claims EC + patta + ROR
+ khata + FMB across 23+ states, plus an AI layer "Terra." Enterprise
sales-gated, no self-serve pricing, TN coverage likely-but-unconfirmed. It would
collapse the hard 80% (the data layer) into an API call. **Evaluate this before
building 5 CAPTCHA-fragile scrapers.**

### Where the two browser-agent tools actually fit

Not the product — the tooling for the **data-acquisition layer of sources 1–5**.
The load-bearing dependency is a **CAPTCHA-solving service**, not the agent
framework. page-agent (via its extension) or plain Playwright drives the forms;
Browser-BC's value is capturing a login+lookup flow as a reusable Claude skill
that fits our skills-based repo. OTP/SSO sources (Tamil Nilam, TNGIS) are best
handled as **user-authenticated / upload** flows — which is exactly what the
dossier locker already does.

### Legal / positioning note

Even LandLens stamps its own report "not legal advice / not a lawyer substitute"
(liability capped at ₹10,000) despite "Legal Opinion" branding, and appears to
rely on portal scraping with no official gov API partnership. Two takeaways:
(1) government-ToS + CAPTCHA-driving is industry-normal but fragile and legally
gray — get this reviewed; (2) a possible differentiator is putting a real
advocate sign-off in the loop.
