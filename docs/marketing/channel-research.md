# Channel research — Instagram + X content pulls

`marketing/research/` pulls **videos + captions + engagement metrics** from a
social channel so the marketing team can study what works before making our
own content (pillar research, hook mining, competitor benchmarking). It
replaces the old ad-hoc Colab notebook — same capability, hardened: env-var
credentials, cached login session, best-effort failure handling, stable CSV
schema, private R2 sharing.

**Read-only, local, on-demand.** Never wire this into CI or a schedule —
Instagram blocks datacenter IPs quickly and repeat logins get accounts
checkpointed.

## Setup

```bash
pip install -r config/requirements.txt   # brings in instaloader + tweepy
```

Env vars (all documented in `.env.example`):

| Var | What |
|---|---|
| `INSTAGRAM_USERNAME` | login account — **burner only, never personal** (see warning) |
| `INSTAGRAM_PASSWORD` | its password; only needed until the first login caches a session |
| `INSTAGRAM_SESSION_FILE` | optional session path (default `~/.config/instaloader/session-<user>`; keep it **outside the repo** — it holds auth cookies) |
| `X_BEARER_TOKEN` | X API v2 bearer token |
| `RESEARCH_OUT_DIR` | optional output root override (default `marketing/research/data` — gitignored; overrides outside ignored paths risk committing scraped media) |
| `R2_*` | the usual R2 credentials (only needed for upload; `--no-upload` works without) |

> **Burner account, seriously.** The notebook this replaces had personal
> credentials hardcoded in source; they leaked and had to be rotated. Use a
> throwaway Instagram account you can afford to lose — scraping accounts get
> checkpointed or banned. Log in from a residential connection.
>
> 2FA accounts: create the session once interactively with
> `instaloader --login <username>`, then this tool reuses it.

The first run logs in and caches a session file; later runs print
`session reused` and skip login entirely — that's what keeps the account
alive. Don't delete the session file between runs.

## Usage

```bash
# latest N video posts (reels) from a channel
python -m marketing.research instagram natgeo --limit 10

# metadata only (no video downloads), local only (no R2)
python -m marketing.research instagram natgeo --limit 5 --metadata-only --no-upload

# skip comment fetching entirely (fastest, least likely to trip rate limits)
python -m marketing.research instagram natgeo --max-comments 0

# one post/reel by URL or shortcode
python -m marketing.research instagram-post https://www.instagram.com/reel/Cxyz123/ --no-upload

# one X post by URL or id
python -m marketing.research x-post https://x.com/SpaceX/status/1234567890
python -m marketing.research x-post 1234567890 --no-replies --no-upload
```

First run of anything new: use `--no-upload` and a tiny `--limit`, inspect the
output, then run for real.

## Output

```
marketing/research/data/{platform}/{channel}/{YYYY-MM-DD}/
    posts.csv     # one row per post, stable column order (below)
    posts.json    # same data, full fidelity (typed, nested)
    run.json      # run metadata: args, counts, timestamp
    media/        # {shortcode|tweet_id}.mp4 / .jpg
```

Same-day re-runs **merge** (deduped by post id, re-pulled post wins) instead
of clobbering.

| Column | Meaning |
|---|---|
| `platform` | `instagram` \| `x` |
| `channel` | profile / handle the post belongs to |
| `post_id` | IG mediaid / tweet id |
| `shortcode` | IG shortcode; empty for X |
| `url` | canonical post URL |
| `posted_at` | ISO-8601 UTC; empty if unknown |
| `caption` | IG caption / X text |
| `likes` | like count (empty = platform didn't expose it) |
| `views` | IG `video_view_count` / X `impression_count` |
| `comments_count` | IG comments / X replies |
| `top_comments` | JSON, max 3 by likes: `[{"username","text","likes"}]` |
| `media_type` | `video` \| `image` \| `none` |
| `media_filename` | basename under `media/`; empty when metadata-only/failed |
| `media_r2_key` | filled after upload |
| `fetch_status` | `ok` \| `partial` (metadata kept, comments/media failed) \| `failed` |
| `error` | human-readable reason when partial/failed |
| `extra` | JSON platform extras (X: retweets/quotes/bookmarks) |

## R2 sharing

Runs upload to the **private** bucket (scraped third-party content must never
be publicly served from our domain) under
`marketing-research/{platform}/{channel}/{YYYY-MM-DD}/…`. The CLI prints each
key plus a **24-hour presigned link** to `posts.csv` for sharing. Mint a fresh
link any time:

```python
from pipeline.storage import presigned_get_url
print(presigned_get_url("marketing-research/instagram/natgeo/2026-07-14/posts.csv", expires_in=86400))
```

## Limitations & failure modes (expected — not bugs)

- **Instagram 403 `login_required` on comments/HD video even when logged in.**
  Routine; the tool degrades to `fetch_status="partial"`, keeps the metadata,
  and records the reason in `error`. `top_comments` is best-effort.
- **X replies** come from recent search: ~7-day window, and recent search is
  unavailable on the free API tier (`partial` + reason). `views`
  (`impression_count`) and mp4 `variants` also vary by tier — when no mp4 is
  exposed the preview image is saved instead.
- **Ban risk etiquette**: small `--limit`, the built-in 2–5s sleeps, don't
  re-run in tight loops, and prefer `--max-comments 0` when you don't need
  comments. If Instagram starts 403ing everything, stop for a day — hammering
  makes it worse.
- Instagram's private API shifts constantly; when instaloader breaks, check
  for an instaloader release before debugging this code.
