# Browser-agent tools for extracting land data from Tamil Nilam (TNGIS)

- **Status:** exploring  <!-- captured | exploring | planned | implemented | parked -->
- **Date added:** 2026-07-06
- **Source:**
  - Browser-BC / "Journey Forge Local" — https://github.com/Einsia/Browser-BC
  - Alibaba page-agent — https://github.com/alibaba/page-agent
  - Target site — https://tngis.tn.gov.in/apps/gi_viewer/index.html
- **Tags:** scraping, browser-automation, gis, tamil-nadu, data-extraction, ai-agent

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

Spike: run **page-agent** against the viewer for a single known survey number
and, in the same session, capture the network traffic to see what map-service
endpoints it hits. That answers the "GUI vs. direct API" question. If it looks
viable and ToU permits, promote to a `docs/design/` spec for a Tamil Nilam
enrichment scraper.
