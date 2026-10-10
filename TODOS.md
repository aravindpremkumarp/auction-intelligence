# TODOS

Organized by component, then priority (P0 highest through P4). Completed items
move to the bottom section with the version they shipped in.

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

Blocks every OpenRouter batch stage — `pipeline/load_extractions.py`
(LangExtract) and `pipeline/classify_document.py` — since they share the key.
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

### Pre-existing test failure on `main` (`test_deferred_capabilities.py`)

**Closed:** 2026-10-10, with the removal of the pydantic-ai chat. The test
covered that agent's deferred-tool mechanism; the agent and the test are both
gone. (It was already passing on `main` by then.)
