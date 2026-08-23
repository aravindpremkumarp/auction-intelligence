# Retiring embeddings from retrieval

**Date:** 2026-08-22
**Status:** implemented
**Supersedes:** the three-vector-index design in `pipeline/embeddings.py`

## Decision

Delete the embedding stack. Retrieval is now **structured filters over the
LangExtract entity graph** plus **two Lucene fulltext indexes**.

## Why

The embedding design predates LangExtract. When descriptions were opaque
blobs, a vector index was the only way to ask anything about their contents.
That is no longer true: LangExtract resolves every notice into typed,
grounded entities — `property_type`, `location.{village,taluk,district}`,
`extent_sqft`, four `boundary` sides, `identifier` by kind,
`reserve_price_num`, `possession_type`, `encumbrance`, plus `extras` for the
long tail.

That changes what the questions *are*:

- **"2-acre agricultural land in Salem under ₹50L"** is a filter, not a
  similarity search. Cypher answers it exactly. A vector answers it
  approximately — which on auction data is not a smaller version of the right
  answer, it is a wrong one. A missed encumbrance or a fuzzy extent is a
  financial mistake, and an approximate ranking cannot be audited or
  explained to a bidder.
- **"borewell", "disputed pathway"** are term-presence questions. Lucene
  answers them exactly. `api/agent3/search_notices.py` already said this in
  its own docstring: a fulltext match "is a precise term-presence question,
  not a semantic-similarity one".

So the vector lenses sat between two things that each did the job better.

Three further facts settled it:

1. **Wrong granularity.** `notice_markdown_idx` and `notice_image_idx` were
   keyed on `Document` — one vector per *notice*. A six-lot notice got one
   vector smearing six unrelated properties together.
2. **Never populated.** `lot_description_embedding`, the one index at the
   right granularity, held zero vectors for its entire life.
3. **Thin source text.** `property_desc_idx` embedded the portal blurb —
   templated boilerplate, and populated for only 2,179 of 2,964 properties.

## What replaced it

`semantic_search` keeps its name and signature — it is wired into v1, v2,
the mode prompts, the matches panel, and stored conversations, and a rename
would break all of them for no gain. Only the engine changed:

| Lens | Index | Source text | Weight |
| --- | --- | --- | --- |
| `schedule` | `lot_description_ft` | `Lot.full_description` — the verbatim description block LangExtract copies out of the notice | 1.0 |
| `description` | `property_text_idx` | `AuctionProperty.title + description` — the portal blurb | 0.85 |

The lot lens is new to this tool and is the real upgrade: it reads the
authoritative text (boundaries, survey numbers, per-side measurements) that
the old vector path never indexed at lot granularity. A lot hit resolves up
to its parent `AuctionProperty` so both lenses rank the same entity.

BM25 scores are unbounded and scale with document length, so the two indexes
are not directly comparable. Each is max-normalized *within its own source*
over the fetched rows, then weighted, then merged by max per property.

### OR, not AND

`_lucene_query` OR-joins terms — the opposite of `search_notices.py`, and
deliberately. That tool answers "does this exact term appear", where AND is
right and OR matches nearly the whole corpus. This one is the broad-recall
lens, so it keeps OR and lets BM25 rank: a lot matching four query terms
scores far above one matching a single common word, without AND's
brittleness. Quoted phrases pass through for callers who want word order.

### `score` changed meaning

Vector cosines were absolute — 0.82 meant the same thing in every result set.
BM25 is unbounded and the two indexes are on different scales, so scores are
now max-normalized *per result set*. The top hit is therefore ~1.0 by
construction, even when nothing matched well, and ties at 1.0 are common and
genuine. `score` ranks rows against each other; it no longer measures
relevance. Both the agent-facing and internal docstrings say so explicitly,
because a model reading 1.0 as "certain" is the obvious failure mode here.

## What got worse, honestly

Matching is now **lexical**. A user who says "godown" when the notice says
"warehouse" gets nothing, where an embedding might have bridged it. Two
things absorb this:

- The agent's job is to translate a question into filters and domain
  vocabulary; the prompts now say the tool matches words, not meaning.
- The zero-result protocol allows exactly one retry that swaps in different
  domain vocabulary (never a reword of the same terms).

If query logs later show real misses that neither filters nor Lucene catch,
that log tells us precisely what to embed — which beats guessing, and is the
condition under which this decision should be revisited.

## Deleted

- `pipeline/embeddings.py`
- `pipeline/embed_descriptions.py`, `embed_markdowns.py`, `embed_notices.py`
- The `lot_description_embedding` vector index from `init_graph_schema.py`
- The Gemini embedding call on the chat tool-executor's hot path

## Not done

- **Orphaned vector data is still in Neo4j.** The `*_embedding` properties and
  the `property_desc_idx` / `notice_markdown_idx` / `notice_image_idx` indexes
  are now unread but still stored. `scripts/drop_embedding_vectors.py` removes
  them; it is deliberately opt-in and requires `--yes`, because it is
  irreversible and the re-embed path no longer exists to undo it. Run it once
  this change has been live long enough to be sure.
- **`google-genai` stays in `requirements.txt`.** Nothing in `api/` imports it
  now, but `langchain-google-genai` pulls it transitively, so dropping the
  direct pin would change nothing in the lock.
- **Image search.** Multimodal retrieval over *site photographs* remains the
  one job structured data genuinely cannot do. Nothing here forecloses it —
  but it should be built at lot granularity over real photos, not over
  notice-PDF bytes, which is what `notice_image_idx` actually held.
