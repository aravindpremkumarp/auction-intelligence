# The page-2 push

**Written 2026-08-19, from the first real Search Console read.
Levers A, B and C all shipped the same day — see "What shipped" below.
The open item is the readout on 2026-09-16.**

The goal of this plan is narrow: take the handful of guide pages that already
earn impressions and move them from page 2 to page 1. It is not a plan to make
more pages. Making more pages is what we have been doing, and the data below is
the argument for stopping.

---

## 1. Where we actually are

Search Console, three months to 2026-08-18 (indexing report dated 2026-08-14):

| | |
|---|---|
| Pages indexed | **684** of 763 known (90%) |
| Not indexed | 79 — of which 74 are "Discovered — currently not indexed" |
| Clicks | **98** |
| Impressions | **3,430** |
| CTR | 2.9% |
| Average position | **18.5** |
| Queries with impressions | 374 |
| Pages with impressions | 318 |

Two facts reframe everything else.

**Indexing is solved.** The Q1 target was 50–150 indexed pages. We have 684.
Nothing in this plan should spend effort on getting indexed.

**Position 18.5 is page 2.** Google is willing to show these pages. It is
showing them where roughly 1% of searchers ever look. Every impression at
position 18 is a near-miss, and we have 3,430 of them.

### Where the clicks come from

| Page | Clicks | Impressions | CTR |
|---|---|---|---|
| `/` (homepage) | 46 | 76 | 60% |
| `/guides/agricultural-land-purchase-rules-tamil-nadu` | 5 | 573 | 0.9% |
| `/guides/town-survey-ts-number-urban-land-records` | 5 | 243 | 2.1% |
| `/guides/prohibited-property-and-poramboke-land-tamil-nadu` | 4 | 51 | **7.8%** |
| `/property/799850` | 4 | 12 | — |
| `/compare/auctionscope-vs-baanknet` | 2 | 109 | 1.8% |
| `/guides/fmb-sketch-tamil-nadu` | 1 | 233 | 0.4% |
| `/guides/fsi-far-in-tamil-nadu` | 1 | 85 | 1.2% |
| `/guides/patta-transfer-and-mutation-tamil-nadu` | 1 | 49 | 2.0% |

The homepage's 46 clicks are people typing "auctionscope" into Google. That is
brand navigation, not SEO working. **Content-earned clicks: ~52 in three
months.**

### The part that should change our minds

The 664 prerendered property pages are 87% of the URL surface. They produced
roughly **6 clicks in three months**. Meanwhile 44 guides produced most of the
impressions on the site.

The 74 URLs sitting in "Discovered — currently not indexed" are Google's
verdict on the same thing: it found those pages, looked at what they offer, and
declined to spend crawl budget. That is a quality signal about thin, templated
pages — not a technical fault to fix.

**Conclusion: generating more pages is not the growth lever. It was a
reasonable bet; the data did not support it.**

---

## 2. The diagnosis

The queries tell us what is wrong, and it is unusually cheap to fix.

| Query | Impr | Clicks | Page that ranks | Its title |
|---|---|---|---|---|
| `ts no full form` | 30 | **0** | TS number guide | "Town survey (TS) number and urban land records in Tamil Nadu" |
| `t s no means` | 21 | **0** | TS number guide | *(same)* |
| `how to read fmb sketch` | 33 | **0** | FMB sketch guide | "FMB sketch — what it is and how to check it (Tamil Nadu)" |
| `how many acres of land can a person own in tamil nadu` | 27 | **0** | Agricultural land guide | "Agricultural land purchase rules in Tamil Nadu" |
| `building setback rules in tamil nadu` | 18 | **0** | Setbacks guide | — |
| `what is uds area` | 18 | **0** | UDS guide | — |

People search in plain, question-shaped language: *full form*, *means*, *how to
read*, *how many acres*. Our titles are written in the language of a property
lawyer: *urban land records*, *purchase rules*, *how to check*.

The answers are genuinely inside these pages. The searcher's own words are not
in the title they see in the results list.

**The counter-example proves it.** The poramboke guide converts at 7.8% CTR —
nearly triple the site average — on only 51 impressions, because its title
happens to be phrased the way people ask. Nothing about that page is
technically better than the others.

So there are two separate problems, and they need different fixes:

- **Position 18.5** — a ranking problem. Slow to fix.
- **Sub-1% CTR even at position 18** — a wording problem. Fixable this week.

---

## 3. The fix, cheapest lever first

### Lever A — match the searcher's words (do this first)

Rewrite `title` and `description` in `scripts/build_guides.py` so the query
appears verbatim. No ranking change required; this converts impressions we
already have.

| Page | Current title | Proposed title | Target query (impr, clicks) |
|---|---|---|---|
| TS number | Town survey (TS) number and urban land records in Tamil Nadu | **TS number full form — what "TS no" means in land records** | `ts no full form` (30, 0) · `t s no means` (21, 0) · `ts no in land full form` (15, 1) |
| FMB sketch | FMB sketch — what it is and how to check it (Tamil Nadu) | **How to read an FMB sketch (Tamil Nadu)** | `how to read fmb sketch` (33, 0) |
| Agricultural land | Agricultural land purchase rules in Tamil Nadu | **How many acres of land can a person own in Tamil Nadu?** | `how many acres of land can a person own in tamil nadu` (27, 0) |
| FSI / FAR | FSI / FAR in Tamil Nadu — how much you can build | **FSI in Tamil Nadu — how much you can build on a plot** | `fsi in tamilnadu` (7, 1) |
| Poramboke | Prohibited property and poramboke land in Tamil Nadu | **leave it alone** | already 7.8% CTR — this is the model, not a target |

Rules while doing this:

- The exact query phrase goes in the `title`, near the front.
- The `description` must answer the question in its first clause, not tease it.
  Someone scanning results should be able to stop reading.
- The `h1` should match the title's promise. Do not let them drift apart.
- Do not manufacture a question the page does not actually answer. If the page
  cannot answer "how many acres", the fix is content, not a title.

Do the same for the two zero-click near-misses: `building setback rules in
tamil nadu` (18 impr) and `what is uds area` (18 impr).

### Lever B — put the answer in the first 60 words

Each of these guides currently opens with context. For a "full form" or "how
many acres" query, the literal answer should be the first sentence on the page,
before any framing. This serves two purposes: it is what a page-1 result reads
like, and it is what gets lifted into a featured snippet or an AI Overview.

Add the exact query as a `FAQPage` question where it is missing. The TS number
guide has "What is a TS number in Tamil Nadu?" but not "What is the full form
of TS no?" — which is the thing being searched.

### Lever C — internal linking (the actual ranking lever)

Every guide currently carries **3 internal links**. That is close to none. 44
guides on closely related Tamil Nadu property topics are sitting next to each
other passing almost no signal.

- Add a "related guides" block to the guide template: 4–6 genuinely related
  guides, chosen per topic, with descriptive anchor text.
- Cross-link inside the body where a term appears — FMB should link to patta,
  patta to EC, EC to guideline value, and so on.
- Link the top guides from the homepage, not just from the `/guides` hub.

This is one change in `scripts/build_guides.py` (a `related: [...]` key per
topic) plus a rebuild.

### Order of work

1. Lever A — titles and descriptions on 6 pages. Half a day.
2. Lever B — lead answers and missing FAQ entries on the same 6. One day.
3. Lever C — related-guides block across all 44. One day.
4. `python -m scripts.build_guides`, review the diff, commit, request
   re-indexing for the 6 changed URLs in Search Console.

---

## 4. What this plan deliberately does not do

- **No new guides** until this reads out. We have 44; we do not know what makes
  one work. Adding a 45th adds noise, not information.
- **No new city landing pages.** The `/bank-auctions/**` hubs barely appear in
  the impressions data. Find out whether they rank at all before expanding to
  more cities.
- **No new property pages.** 664 pages, ~6 clicks. Regenerate for freshness;
  do not expand the count.
- **No chasing international traffic.** US, Philippines, Thailand, Indonesia,
  Vietnam and Turkey impressions come from generic phrases like "ts no full
  form" matching unrelated searches. They will never convert. India is 94 of 98
  clicks and that is correct.

---

## 5. How we will know it worked

Re-read Search Console **2026-09-16** (four weeks). Compare like for like, on
the same 3-month window shape.

| Metric | Now | Success | Verdict if missed |
|---|---|---|---|
| CTR on the 6 rewritten pages | 0.4–2.1% | **≥4%** | Title wording is not the constraint — go to Lever C and content depth |
| Average position, sitewide | 18.5 | **≤14** | Internal linking is insufficient; the pages need real depth or external links |
| Content-earned clicks / 3 mo | ~52 | **≥150** | — |
| Clicks per indexed page | 0.14 | **≥0.3** | — |

**The counterfactual, stated in advance:** if CTR on the rewritten pages does
not move despite the titles now matching the queries exactly, then the problem
is not presentation — it is that position 18 gets almost no eyeballs regardless
of wording, and the honest next step is a smaller number of much deeper pages,
or off-site authority. It is not another round of the same thing.

---

## 6. Open questions worth a look

- **Desktop CTR is 1.1% against mobile's 4.7%** on near-identical impressions
  (1,722 vs 1,683). Same pages, same queries, a quarter of the clicks. Either
  desktop rankings are materially worse or the desktop result presentation is
  losing people. Not yet diagnosed.
- **Impressions fell sharply after 2026-08-12.** If it persists two weeks it is
  a ranking movement, not sampling noise. Check before drawing conclusions from
  the September read.
- **What are the 664 property pages for?** They may be doing a real job — a
  crawlable inventory surface, something to cite, a landing target for direct
  links — that simply is not a search-traffic job. Worth deciding explicitly
  rather than leaving them as an unexamined cost.

---

## Mechanics

Guides and comparison pages are built locally and committed — they are not in
`seo-pages.yml`, which only regenerates property and landing pages:

```
python -m scripts.build_guides     # writes /guides/** + rebuilds sitemap
python -m scripts.build_compare    # writes /compare/** + rebuilds sitemap
```

Content lives in the `GUIDES` list in `scripts/build_guides.py` — one dict per
topic (`slug`, `title`, `h1`, `description`, body, `faqs`). The renderer is
generic; adding a `related` key means touching the renderer once.


---

## What shipped (2026-08-19)

All three levers, in `scripts/build_guides.py`, regenerated into `web/guides/`.

**Lever A — titles matched to query wording.** Six guides retitled; the
poramboke guide deliberately untouched.

| Page | Now titled | Target query |
|---|---|---|
| TS number | TS number full form — what "TS no" means in Tamil Nadu land records | `ts no full form` |
| FMB sketch | How to read an FMB sketch (Tamil Nadu) | `how to read fmb sketch` |
| Agricultural land | How many acres of land can a person own in Tamil Nadu? | `how many acres of land can a person own in tamil nadu` |
| FSI / FAR | FSI in Tamil Nadu — how much you can build on a plot | `fsi in tamilnadu` |
| Setbacks | Building setback rules in Tamil Nadu | `building setback rules in tamil nadu` |
| UDS | What is UDS area in a flat? Undivided share explained | `what is uds area` |

Descriptions and `h1`s moved with the titles, and each `answer` now leads with
the literal answer — the acreage ceiling, "TS stands for Town Survey" — rather
than framing.

**Lever B — the searched phrasing in the FAQs.** Added "What is the full form
of TS number?", "How many acres of land can a person own in Tamil Nadu?" and
"What is UDS area?" alongside the existing formal versions. These land in the
`FAQPage` JSON-LD, which is what a featured snippet or AI Overview lifts.

**Lever C — internal linking.** A related-guides block now links each guide
out: curated for the six targets, falling back to hub-group siblings
everywhere else so all 44 gain links, not just the hand-tended few. Anchor
text is the target guide's `h1`. Measured **3 → 9 internal links per guide**.

Worth naming why the count had been stuck at 3: the existing "related terms"
block rendered `<span class="chip">` — plain text, no `href`. It looked like
internal linking and passed no signal at all.

A build-time guard fails the build if `related_guides` names a slug that does
not exist, since the renderer skips unknown slugs silently and would otherwise
cost the link without anyone noticing.

**Still open:** the 2026-09-16 readout in section 5. Nothing in section 4
("what this deliberately does not do") has changed — no new guides, cities or
property pages until that reads out.
