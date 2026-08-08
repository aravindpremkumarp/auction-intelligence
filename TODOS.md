# TODOS

Organized by component, then priority (P0 highest through P4). Completed items
move to the bottom section with the version they shipped in.

## Test suite

### Pre-existing test failures on `main` (8)

**Priority:** P0
**Noticed:** 2026-08-09, on branch `claude/pull-latest-main-f081ff` (gstack /ship)

Eight tests fail on clean `main`. Confirmed pre-existing by stashing the branch
diff and re-running — identical failure set, so they are not caused by the
review/pipeline fixes shipped on that branch.

They matter beyond their own coverage: a permanently red suite means the next
regression is indistinguishable from the standing noise.

| Test | File |
|---|---|
| `test_chat_stream_happy_path` | `tests/api/test_chat_stream.py` |
| `test_deferred_tools_hidden_on_first_request` | `tests/api/test_deferred_capabilities.py` |
| `test_catalog_rides_in_instructions` | `tests/api/test_deferred_capabilities.py` |
| `test_loaded_capability_survives_history_replay` | `tests/api/test_deferred_capabilities.py` |
| `test_load_capability_returns_cypher_instructions` | `tests/api/test_deferred_capabilities.py` |
| `test_agent_tool_accepts_str_or_list` | `tests/api/test_detail_batching.py` |
| `TestBuildIsland::test_live_auction` | `tests/scripts/test_property_og.py` |
| `test_missing_env_raises_configured_error` | `tests/test_storage.py` |

Lead on the chat/capabilities cluster (5 of the 8) — they share one error:

```
chat.agent_run status=error mode=ask model=flash
err=TypeError("'async_generator' object does not support the asynchronous
context manager protocol (missed __aexit__ method)")
agent stream failed for message='auctions in chennai'
```

That reads like an `async with` applied to a bare async generator — most likely
agent-SDK drift where a helper stopped being an async context manager. Fixing
that one shape probably clears the chat_stream, deferred_capabilities, and
detail_batching failures together. `test_property_og` and `test_storage` look
independent.

Repro:

```bash
python -m pytest tests/api/test_chat_stream.py tests/api/test_deferred_capabilities.py tests/api/test_detail_batching.py tests/scripts/test_property_og.py tests/test_storage.py -q
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

### `OPENROUTER_API_KEY` malformed in `.env`; classifier fails silently

**Priority:** P0
**Noticed:** 2026-08-09

Line 5 of `.env` reads `OPENROUTER_API_KEY=OPENROUTER_API_KEY=sk-or-v1-…` — the
variable name is duplicated inside its own value, so every request sends
`Authorization: Bearer OPENROUTER_API_KEY=sk-or-…` and OpenRouter answers 401
"Missing Authentication header". `OPENROUTER_CHAT_API_KEY` is well-formed, which
is why chat works and the pipeline does not.

`pipeline/classify_notice.py` pass 2 failed **1192 of 1192** documents this way
with no error line printed: a non-200/404/403 response retries `MAX_RETRIES`,
falls out of the loop, returns `None`, and increments `failed` without logging
the status or body. Add the status code to that path.

Blocks `pipeline/extract_descriptions.py` and `pipeline/load_extractions.py`
too — same key.

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
