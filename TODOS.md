# TODOS

Organized by component, then priority (P0 highest through P4). Completed items
move to the bottom section with the version they shipped in.

## Test suite

### Pre-existing test failure on `main` (1, was 8)

**Priority:** P2 (was P0)
**Noticed:** 2026-08-09, on branch `claude/pull-latest-main-f081ff` (gstack /ship)
**Updated:** 2026-08-14 — 7 of the 8 now pass; only the leak check remains.

One test still fails on clean `main`:

| Test | File |
|---|---|
| `test_deferred_tools_hidden_on_first_request` | `tests/api/test_deferred_capabilities.py` |

It asserts that deferred tools stay out of the always-sent tool surface, and
fails because `run_cypher` and `describe_schema` are present on the first
request:

```
AssertionError: deferred tools leaked into the always-sent surface:
{'run_cypher', 'describe_schema'}
```

So this is no longer the async-context-manager drift the original entry
diagnosed — that cluster (chat_stream, the other three deferred_capabilities
tests, detail_batching) is fixed, as are `test_property_og` and `test_storage`.
Either the two tools were deliberately promoted to always-sent and the test was
not updated, or the deferral genuinely regressed. Answering that is the task.

Priority drops from P0 to P2: one known-red test is still noise against the next
regression, but the suite is legible again (1,155 passing).

Repro:

```bash
python -m pytest tests/api/test_deferred_capabilities.py -q
```

## Pipeline

### Renew `MINERU_API_KEY` and make OCR failures loud

**Priority:** P0
**Noticed:** 2026-08-09

`MINERU_API_KEY` expired 2026-08-01. `scripts/ocr_missing_markdowns.py` ran all
19 batches against MinerU, got 401 on every one, OCR'd 0 of 189 documents — and
**exited 0**. Stage 1 of `run_weekly_pipeline.py` will keep silently doing
nothing until the key is replaced.

Two separate fixes:
1. Renew the key (MinerU tokens are short-lived JWTs; this one lasted ~14 days).
2. Make the script exit non-zero when every batch fails, so cron/CI can detect
   it. Right now "exit 0" is what an automated caller would trust.

Same class of bug as the OpenRouter key below — silent credential failure
reported as success.

### `OPENROUTER_API_KEY` malformed in `.env`; batch stages fail silently

**Priority:** P0
**Noticed:** 2026-08-09

Line 5 of `.env` reads `OPENROUTER_API_KEY=OPENROUTER_API_KEY=sk-or-v1-…` — the
variable name is duplicated inside its own value, so every request sends
`Authorization: Bearer OPENROUTER_API_KEY=sk-or-…` and OpenRouter answers 401
"Missing Authentication header". `OPENROUTER_CHAT_API_KEY` is well-formed, which
is why chat works and the pipeline does not.

It fails **silently**: a non-200/404/403 response retries `MAX_RETRIES`, falls
out of the loop, returns `None`, and increments `failed` without logging the
status or body. Add the status code to that path.

Blocks every OpenRouter batch stage — `pipeline/ocr_extract.py` and
`pipeline/load_extractions.py` (LangExtract) — since they share the key.
(First diagnosed via `classify_notice.py`'s LLM pass, which failed 1192 of 1192
documents this way; that pass has since been removed — classification is now
cluster count + human review.)

### Two truncated notice JPEGs in R2

**Priority:** P3
**Noticed:** 2026-08-09

`tfl-3-17826285417943.jpg` (auction 802057) and `CHOLAMNDL17797969294386.jpg`
(auction 777450) are stored incomplete in R2: valid JFIF header, no `FF D9`
end-of-image marker, and both sizes are exact 4 KB multiples (1,220,608 and
368,640) — a write that stopped on a block boundary. R2's `Content-Length`
matches the local byte count, so the download is faithful and re-running the
uploader will not help. Datalab rejects both with "Could not open the input
image". Needs a re-scrape from the source, or re-upload from an intact local
copy if one exists.

## Completed
