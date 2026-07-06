# End goal: LandLens-style automated legal opinion for any TN property

- **Status:** planned  <!-- captured | exploring | planned | implemented | parked -->
- **Date added:** 2026-07-06
- **Source:** https://verified.realestate/landlens/demo/legal-opinion
- **Tags:** end-goal, legal-opinion, due-diligence, tn-land-records, scoring, dossier
- **Related:** `2026-07-06-browser-agents-for-tngis-extraction.md` (data-acquisition tooling for one input)

## What it is

LandLens (Verified.RealEstate, Chennai proptech, RERA agent TN/Agent/0181/2024)
auto-generates a due-diligence "legal opinion" report for a TN property:
0–100 risk score + verdict ("Proceed with Caution", 38/100 in the demo), title
chain since 1992 from deeds+EC, patta/EC cross-verification, CERSAI mortgage
search, eCourts litigation search, GIS zone screening (HT lines, water buffers,
poramboke), severity-ranked issues (critical/moderate/minor), and prioritized
recommendations. **Our end goal: produce this for any given property.**

Competitive facts worth remembering:

- Two tiers: full AI Legal Opinion (survey no. + district/taluk + optional
  uploads) and a lighter "Verify My Land" (map click, ~30 automated checks).
- Pricing: ~₹999/full report (10 credits), ~₹340–400/quick verify,
  ₹9,999/mo enterprise. Marketed "minutes", FAQ admits 2–4h, up to 1–3 days.
- Claims ~90% automated; record access is portal automation (no disclosed
  official API partnerships) — the same fragile foundation we'd build on.
- Despite the branding, its disclaimer says it does **not** provide legal
  advice, recommends an independent lawyer, caps liability at ₹10k.
  **Differentiation opening: a real advocate sign-off in the loop.**

## Where we already are (survey of our codebase, 2026-07-06)

This is **Phase 2 of a feature we already designed**: the dossier design
(`docs/design/2026-06-13-document-dossier-ai-analysis.md`) is explicitly
"locker-first, verdicts-later" — its named Phase 2 *is* the AI legal opinion.

Already built and reusable:

- **Join keys extracted from every sale notice** (MinerU + LangExtract):
  survey nos (old/new), patta/chitta/khata nos, CERSAI IDs, sale-deed nos,
  boundaries, encumbrance disclosures, DRT/IBC court refs, borrowers/heirs,
  village/taluk/district. Caveat: they live inside `Document.extraction_json`
  spans, not as first-class queryable property fields (SurveyNumber node was
  removed 2026-05) — a normalization/promotion step is prerequisite work.
- **Dossier v1 shipped dark** (`DOSSIERS_ENABLED=false`): 9-category/~50-type
  TN diligence taxonomy + 0–100 Diligence Readiness Score (`api/dossier/`).
- **The right scoring pattern exists**: `pipeline/validators.py` does
  100-minus-severity-penalties over typed `{code, severity, msg}` issues —
  structurally identical to LandLens's verdict; just pointed at extraction
  quality today. (The 10-dimension investment scorer is the wrong template —
  uncalibrated heuristics, demoted from the chat agent.)
- **Report generation precedent**: deep-research chat mode emits a 7-section
  due-diligence report (ephemeral, in-chat only — no stored artifact/PDF).
- **Eval machinery** (golden questions + extraction gold sets with
  human-review→gold flywheel) to hold a title-risk score to an accuracy bar.

Missing entirely: government-record connectors (zero code, reference docs
only), EC transaction parsing, title-chain construction, cross-document
reconciliation, a legal-risk issues engine, stored/shareable report artifacts.

## Data-source feasibility (verified 2026-07-06, probed where possible)

None of the six official sources has a public developer API or open data —
every one is a human-facing web form, so "automation" = headless browser +
CAPTCHA handling, for all of them.

| # | Source | Gate | Verdict |
|---|--------|------|---------|
| 1 | Guideline value (TNREGINET) | CAPTCHA only, no login | Easiest |
| 2 | Encumbrance Certificate (TNREGINET) | CAPTCHA, free view, no login | Scriptable |
| 3 | Patta/Chitta/A-Register/FMB view (eservices.tn.gov.in) | CAPTCHA; resists plain HTTP; Tamil Nilam authoritative data is OTP-walled | Browser-agent |
| 4 | CERSAI public search | CAPTCHA + ₹10+GST per search | Scriptable, paid per query |
| 5 | eCourts case search (party-name works) | CAPTCHA, no official API; resellers exist (eCourtsIndia, Vakeel360, Surepass) | Hard DIY or buy |
| 6 | TNGIS GIS layers | **TN-SSO login wall — sealed** | Manual / user-authenticated only |

TNGIS detail (probed directly): the viewer fronts TN Single Sign-On; map data
flows through a token-gated custom API (`/apps/gi_viewer_api/gi_mvc/api/v1`);
standard `/arcgis/rest/services` and `/geoserver/wms` paths return 404
unauthenticated; a community GIS index confirms the GeoServer "needs cookies
from public login." Login-free GIS fallback: ISRO Bhuvan / NIC national WMS
layers (`bhuvan-vec1.nrsc.gov.in/bhuvan/wms`) — land use/water bodies but not
TN parcel cadastre.

**Buy-vs-build shortcut:** Landeed (already catalogued in
`docs/landeed_tn_records.md`) now ships a Document Procurement API (survey-no
keyed; "EC, patta, ROR, khata, FMB"; 