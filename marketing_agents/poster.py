"""Agent A — "The Poster": draft social posts from live auction data.

Runs AFTER each data refresh (manual workflow_dispatch — see
.github/workflows/content-poster.yml), never on a fixed clock, so drafts
always reflect fresh data. Spec: docs/marketing/content-agents.md.

Tier-1 only: this module DRAFTS and STAGES. It never publishes anywhere.
Output lands in marketing/outputs/YYYY-MM-DD/ for human review.

Two engines can write the drafts (see the "pipeline steps" section below):
  * Claude Code on the founder's Max subscription — the DEFAULT in CI
    (no per-token API cost). The workflow runs --prepare, then `claude -p`
    turns <work>/prompt.txt into <work>/response.txt, then --finalize.
  * OpenRouter — the API fallback: --generate does prompt.txt -> response.txt,
    or run with no stage flag for the original single-shot behavior.

Env:
    API_BASE                 auction API base (default: production Render URL)
    OPENROUTER_CHAT_API_KEY  preferred key (the valid one production chat uses);
                             falls back to OPENROUTER_API_KEY, matching
                             api/main.py / pipeline/config.py. The plain
                             OPENROUTER_API_KEY repo secret is stale (401) —
                             see the comment in .github/workflows/golden.yml.
                             Only needed for the OpenRouter engine.
    OPENROUTER_MODEL         chat model (default: deepseek/deepseek-v4-flash)
    AGENTS_ENABLED           kill switch — anything but "false" means enabled
    POSTER_OUT_DIR           output root (default: marketing/outputs)
    POSTER_WORK_DIR          staged-run scratch dir (default: .poster_work)

Usage:
    python -m marketing_agents.poster --prepare   # stage 1: data -> prompt.txt
    python -m marketing_agents.poster --generate  # stage 2 (OpenRouter fallback)
    python -m marketing_agents.poster --finalize  # stage 3: validate + stage
    python -m marketing_agents.poster             # single-shot (OpenRouter)
    python -m marketing_agents.poster --dry-run   # fetch + shape only, no LLM
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import httpx

API_BASE_DEFAULT = "https://auction-api-w68b.onrender.com"
MODEL_DEFAULT = "deepseek/deepseek-v4-flash"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
PRODUCT_MARKETING_PATH = Path(".agents/product-marketing.md")

# Honesty rule (docs/marketing/plan.md): drafts must never promise legal
# certainty. A draft containing any of these is dropped, not fixed.
BANNED_PATTERNS = [
    r"\bdue[- ]diligence\b", r"\bdiligence\b", r"\badvocate\b",
    r"\blegal opinion\b", r"\btitle[- ]clear\b", r"\bguaranteed?\b",
    r"\binstitutional\b", r"\brevolutionary\b",
]

MAX_POST_WORDS = 220

# "Prove It" quality gate (docs/marketing/copy-playbook.md Part 3): the number
# does the work, so a post with no concrete figure — a price, a date, a digit —
# is weak copy and gets dropped. Matches any digit (₹40L, 15%, "1 Aug", "2024").
HAS_FIGURE = re.compile(r"\d")

# Hook gates (copy-playbook.md Part 1, "the stop test"). Objective slices only;
# whether the hook actually opens a curiosity gap stays a human/model judgment.
MAX_HOOK_CHARS = 100  # Instagram folds captions ~125 chars; hooks must survive it
MAX_HEADLINE_CHARS = 64  # image_headline is burned onto the card; keep it on ~2 lines
BANNED_OPENERS = (
    "did you know", "attention", "imagine", "are you looking",
    "introducing", "we're excited", "we are excited",
    "don't miss", "dont miss", "hurry", "last chance",
)
HOOK_MECHANISMS = ("contrast", "question", "mistake", "hidden",
                   "myth", "callout", "countdown", "process")
MAX_PER_MECHANISM = 2  # variety rule: a batch may not lean on one mechanism

# Reel gates (copy-playbook.md Part 6; the deal-reel template's on-screen
# budgets). The reel hook is the video's first frame — it must survive a
# 1080px-wide 9:16 canvas at display size, so the budgets are tight.
MAX_REEL_HOOK_L1_CHARS = 18   # the ₹ figure line, 150px mono on the canvas
MAX_REEL_HOOK_L2_CHARS = 28   # the gap line, 112px display type
MAX_REEL_LINE_CHARS = 30      # each curiosity-gap context line
MAX_REEL_SAVE_CHARS = 40      # the end-card save-trigger line


# ---------------------------------------------------------------- data layer

def fetch_json(client: httpx.Client, path: str, params: dict | None = None) -> dict:
    resp = client.get(path, params=params or {})
    resp.raise_for_status()
    return resp.json()


def fetch_pool(api_base: str) -> tuple[dict, list[dict]]:
    """Pull /stats + three angle queries; return (stats, deduped candidates)."""
    today = date.today().isoformat()
    with httpx.Client(base_url=api_base, timeout=60) as client:
        stats = fetch_json(client, "/stats")
        closing_soon = fetch_json(
            client, "/properties",
            {"date_from": today, "sort": "date_asc", "limit": 25},
        )["results"]
        cheapest = fetch_json(
            client, "/properties",
            {"date_from": today, "sort": "price_asc", "limit": 15},
        )["results"]
        # Wider upcoming page: source for price-drop (re-auction) candidates.
        upcoming = fetch_json(
            client, "/properties",
            {"date_from": today, "sort": "date_asc", "limit": 200},
        )["results"]
    drops = [r for r in upcoming if _is_price_drop(r)]
    return stats, shape_candidates(closing_soon, drops, cheapest)


def _is_price_drop(row: dict) -> bool:
    prev, cur = row.get("previous_reserve_price"), row.get("reserve_price")
    return bool(row.get("is_reauction")) and prev and cur and prev > cur


def shape_candidates(closing: list[dict], drops: list[dict], cheapest: list[dict]) -> list[dict]:
    """Tag angles, dedupe by auction_id (first angle wins), keep prompt fields."""
    pool: dict[str, dict] = {}
    for angle, rows in (("price_drop", drops), ("closing_soon", closing), ("cheapest", cheapest)):
        for row in rows:
            aid = row.get("auction_id")
            if not aid or aid in pool or not row.get("reserve_price"):
                continue
            cand = {
                "auction_id": aid,
                "angle": angle,
                "title": (row.get("title") or "")[:140],
                "city": row.get("city"),
                "area": row.get("area"),
                "bank": row.get("bank_short") or row.get("bank"),
                "property_types": row.get("property_types") or [],
                "asset_category": row.get("asset_category"),
                "reserve_price": row.get("reserve_price"),
                "reserve_lakhs": round(row["reserve_price"] / 1e5, 1),
                "emd": row.get("emd"),
                "auction_start": row.get("auction_start"),
                "url": row.get("url"),
            }
            if angle == "price_drop":
                prev = row["previous_reserve_price"]
                cand["previous_reserve_price"] = prev  # raw ₹, for the card island
                cand["previous_reserve_lakhs"] = round(prev / 1e5, 1)
                cand["drop_pct"] = round(100 * (prev - row["reserve_price"]) / prev, 1)
            pool[aid] = cand
    # Price drops first (best content), then soonest deadlines, then cheapest.
    order = {"price_drop": 0, "closing_soon": 1, "cheapest": 2}
    ranked = sorted(pool.values(), key=lambda c: (order[c["angle"]], c["auction_start"] or ""))
    return ranked[:24]  # plenty for the model to choose from, small enough to ground


def resolve_api_key(env: dict | None = None) -> str | None:
    """Prefer the chat key production uses; fall back to the legacy key.

    Mirrors api/main.py / pipeline/config.py. The plain OPENROUTER_API_KEY
    repo secret is stale and 401s (documented in golden.yml).
    """
    env = env if env is not None else os.environ
    return env.get("OPENROUTER_CHAT_API_KEY") or env.get("OPENROUTER_API_KEY")


# ------------------------------------------------------------- prompt layer

def load_brand_context() -> str:
    """Brand voice + customer language from product-marketing.md (best effort)."""
    try:
        text = PRODUCT_MARKETING_PATH.read_text(encoding="utf-8")
    except OSError:
        return "Tone: understated, plain-spoken, lowercase, calm, no hype."
    sections = []
    for heading in ("## Brand Voice", "## Customer Language"):
        idx = text.find(heading)
        if idx != -1:
            end = text.find("\n## ", idx + 1)
            sections.append(text[idx : end if end != -1 else len(text)])
    return "\n\n".join(sections) or text[:2000]


def build_prompt(stats: dict, candidates: list[dict], max_drafts: int,
                 recent_performance: str = "") -> str:
    perf_block = ""
    if recent_performance:
        perf_block = f"""
RECENT PERFORMANCE (Agent B's last report — bias, don't obey blindly):
{recent_performance}
Bias mechanism and angle choices toward what the report scales; the variety
cap (max 2 per mechanism) still applies.
"""
    return f"""You are the social content drafter for AuctionScope (auctionscope.in) —
an AI research assistant for Indian bank-auction (SARFAESI) property in Tamil Nadu.

BRAND CONTEXT (follow the voice; respect words-to-avoid strictly):
{load_brand_context()}
{perf_block}

HARD RULES:
- Use ONLY the auction data below. Never invent prices, dates, counts, or places.
- Every draft must reference exactly one auction by its auction_id.
- Numbers in the post must match that auction's data (₹ lakhs as given).
- Never use: "due diligence", "advocate", "legal opinion", "title-clear",
  "guaranteed", "institutional", "revolutionary". We help people research and
  evaluate; we do not provide legal certainty.
- English, under {MAX_POST_WORDS} words per post, no corporate cliches, no emoji spam
  (one emoji max). Each post ends with a soft pointer to auctionscope.in.

HOOK SYSTEM (docs/marketing/copy-playbook.md Part 1) — the first line is the hook,
and it is engineered, not decorated. The winning formula is SPECIFIC BUT INCOMPLETE:
give the concrete fact (credibility), hold back the why/how/cost (pull). Never
resolve the hook inside the hook.

THE STOP TEST — a hook passes all four or it is not a hook:
1. STOP — breaks the feed pattern: a ₹ figure, a contrast, a sharp question. No warm-up.
2. YOU  — the target buyer feels addressed (their city, budget, risk, identity).
3. GAP  — opens ONE specific question that the body answers.
4. TRUE — every word from the auction's data; deadlines are facts, never hype.
          The body must close the loop the hook opened — if the facts can't cash
          the hook's promise, shrink the hook; never inflate the body.

FORM (enforced in code): the hook goes on its own first line, <=100 characters,
then a blank line, then the body. Never open with: did you know / attention /
imagine / are you looking for / introducing / we're excited / don't miss /
hurry / last chance.

MECHANISMS (rotate — max 2 drafts per mechanism per batch, code-enforced):
- contrast:  "₹45L → ₹38L. same <city> plot, two months apart."          (price_drop)
- question:  "would you bid ₹38L on a plot you've only seen as a PDF?"
- mistake:   "a ₹38L plot is not a deal if the area floods every november."
- hidden:    "banks in TN are selling <live> properties right now. the list is
             public. almost nobody reads it."
- myth:      "you don't need ₹38L in cash to bid — the EMD here is ₹3.8L."
- callout:   "hunting a plot in <city> under ₹40L? a bank just listed one at ₹38L."  (cheapest)
- countdown: "<n> days left. someone gets this <city> <type> at ₹<now>L. did
             anyone check the flood map?"                                 (closing_soon)
- process:   "why is this flat ₹7L cheaper the second time the bank auctions it?"  (re-auction)

BODY after the hook + blank line: stakes (the "so what" for a bidder) -> the facts
(numbers do the work) -> context (vs last listing / what to check) -> one soft CTA.

PER POST: draft 3 candidate hooks using 3 DIFFERENT mechanisms, judge them against
the stop test, lead with the winner, and return the two runners-up in
hook_alternatives (they must also be grounded and honest — the editor may swap).

QUALITY BAR (every draft must pass):
1. Clarity  — one idea, reads in one pass.
2. Prove It — the post MUST contain a concrete figure (a ₹ price or a date). The
              number does the work; adjectives do not.
3. So What  — answer "so what?" for a bidder (e.g. "₹38L" → "₹7L under its last listing").
4. Voice    — lowercase, calm, no hype, no brochure cliches.
Avoid AI slop: no "amazing/incredible/unlock/don't miss out". Let the noun and the
number carry it.

SITE SNAPSHOT: {stats.get("upcoming_auctions")} live auctions of {stats.get("total_auctions")} tracked (as of {stats.get("generated_at")}).

CANDIDATE AUCTIONS (JSON):
{json.dumps(candidates, ensure_ascii=False, indent=1)}

TASK: pick the {max_drafts} most interesting candidates (prefer price_drop angles,
then closing_soon, then cheapest; vary cities AND vary hook mechanisms) and write
one post draft each, using the hook system and passing the quality bar above.

POST LAYERS (docs/marketing/copy-playbook.md Part 6) — write every layer:
- post: the caption; its FIRST LINE is the hook (must land before "…more").
- pinned_comment: REQUIRED (a draft without it is dropped) — the link
  (auctionscope.in) + an honest disclaimer ("not legal advice — a SARFAESI bank
  e-auction; verify reserve, EMD, possession, encumbrances with the bank before
  bidding") + one genuine engagement question. Same honesty rules as the caption
  (no banned words).
- alt_text: <=125 chars, plainly describes the deal card for screen readers.
- video_title: <=70 chars, keyword-first, for Shorts/YouTube search.
- location_tag: the city/area, for local discovery.
- hashtags: 3-5, no # prefix — 1 category, 1-2 niche, 1 geo (matches location_tag),
  1 branded (auctionscope).

REEL (the 12s deal-reel video; docs/marketing/copy-playbook.md Part 6):
Score your 3 candidate hooks 0-4 on the stop test (1 point per leg). The
caption already leads with the winner; ALSO compress the strongest into the
reel's FIRST FRAME as reel_hook — this is what stops the scroll in the first
second, so it is the most important copy you write:
- reel_hook.line1: the ₹ figure, <= {MAX_REEL_HOOK_L1_CHARS} chars, MUST contain a digit
  (e.g. "₹45L → ₹38L" or "₹7.5L"). The number does the work.
- reel_hook.line2: the gap, <= {MAX_REEL_HOOK_L2_CHARS} chars, opens the question the reel
  answers (e.g. "nobody bid." / "cheapest in salem."). No warm-up words.
- reel_context_lines: EXACTLY 2 lines, each <= {MAX_REEL_LINE_CHARS} chars, shown mid-reel.
  They deepen the curiosity and must NOT resolve it (the money reveal does).
- engagement_question: one genuine question ending in "?" — the reel's end
  card. Ask the SAME question the pinned_comment asks, so comments flow into
  a thread you already answered.
- save_line: <= {MAX_REEL_SAVE_CHARS} chars, a FACTUAL reason to save (a date or price:
  "bids close 24 jul — save this"), never manufactured urgency.
- needs_reel: true when this auction carries a strong visual story (always
  true for price_drop); false is fine for a weak candidate.

OUTPUT — ONLY valid JSON, no prose, no code fences:
{{
  "drafts": [
    {{
      "auction_id": string,           // must match a candidate exactly
      "angle": string,                // price_drop | closing_soon | cheapest
      "hook_mechanism": string,       // contrast | question | mistake | hidden | myth | callout | countdown | process
      "hook_alternatives": [string],  // the 2 runner-up hooks (grounded, honest)
      "post": string,                 // hook on line 1 (<=100 chars), blank line, body
      "pinned_comment": string,       // link + honest disclaimer + a question
      "hashtags": [string],           // 3-5, no # prefix
      "alt_text": string,             // <=125 chars, describes the image
      "video_title": string,          // <=70 chars, keyword-first (Shorts/YouTube)
      "location_tag": string,         // city/area for local discovery
      "needs_image": boolean,
      "image_headline": string,       // <=8 words: the hook compressed, same mechanism
      "needs_reel": boolean,          // true for strong visual stories (always for price_drop)
      "reel_hook": {{"line1": string, "line2": string}},  // first frame: figure + gap
      "reel_context_lines": [string, string],             // mid-reel curiosity lines
      "engagement_question": string,  // end card; same question as pinned_comment
      "save_line": string             // factual save reason (date/price)
    }}
  ],
  "editor_notes": string
}}"""


def call_llm(prompt: str, api_key: str, model: str) -> str:
    resp = httpx.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "max_tokens": 4000,  # 3 candidate hooks per draft need headroom
            "temperature": 0.7,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def parse_llm_json(raw: str) -> dict:
    """Parse the model reply; tolerate stray code fences / leading prose."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model reply")
    return json.loads(text[start : end + 1])


# --------------------------------------------------------------- validation

def extract_hook(post: str) -> str:
    """The hook is the post's first line; for single-paragraph posts, its first
    sentence. A dot needs trailing whitespace to end a sentence, so decimals
    (₹40.1L) never split."""
    first_line = post.strip().split("\n", 1)[0].strip()
    if len(first_line) <= MAX_HOOK_CHARS:
        return first_line
    m = re.match(rf"(.{{1,{MAX_HOOK_CHARS}}}?[.!?])\s", first_line)
    return m.group(1) if m else first_line


def _reel_gate_reason(d: dict, reel_hook: dict) -> str | None:
    """Objective reel gates (copy-playbook.md Part 6). Returns the rejection
    reason, or None when the reel copy passes. Only called when needs_reel."""
    l1 = (reel_hook.get("line1") or "").strip()
    l2 = (reel_hook.get("line2") or "").strip()
    if not l1 or not l2:
        return "needs_reel but reel_hook is incomplete (line1 + line2 required)"
    if not HAS_FIGURE.search(l1):
        return "reel_hook.line1 has no figure (the number does the work on frame 0)"
    if len(l1) > MAX_REEL_HOOK_L1_CHARS:
        return (f"reel_hook.line1 is {len(l1)} chars "
                f"(>{MAX_REEL_HOOK_L1_CHARS} — won't fit the first frame)")
    if len(l2) > MAX_REEL_HOOK_L2_CHARS:
        return (f"reel_hook.line2 is {len(l2)} chars "
                f"(>{MAX_REEL_HOOK_L2_CHARS} — won't fit the first frame)")
    lead2 = re.sub(r"^[^0-9a-zA-Z₹]+", "", l2).lower()
    if lead2.startswith(BANNED_OPENERS):
        return "reel_hook.line2 opens with throat-clearing (fails the stop test)"
    ctx = [c for c in (d.get("reel_context_lines") or []) if (c or "").strip()]
    if len(ctx) != 2:
        return "reel_context_lines must be exactly 2 non-empty lines"
    for c in ctx:
        if len(c) > MAX_REEL_LINE_CHARS:
            return (f"reel context line is {len(c)} chars "
                    f"(>{MAX_REEL_LINE_CHARS} — won't fit mid-reel)")
    q = (d.get("engagement_question") or "").strip()
    if not q.endswith("?"):
        return "engagement_question missing or doesn't end with '?' (the end card is a question)"
    save = (d.get("save_line") or "").strip()
    if len(save) > MAX_REEL_SAVE_CHARS:
        return (f"save_line is {len(save)} chars (>{MAX_REEL_SAVE_CHARS})")
    if save:
        lead_s = re.sub(r"^[^0-9a-zA-Z₹]+", "", save).lower()
        if lead_s.startswith(BANNED_OPENERS):
            return "save_line opens with manufactured urgency (banned opener)"
    return None


def validate_drafts(drafts: list[dict], candidates: list[dict]) -> tuple[list[dict], list[str]]:
    """Enforce grounding + the honesty rule. Violators are dropped, not fixed."""
    by_id = {c["auction_id"]: c for c in candidates}
    banned = [re.compile(p, re.IGNORECASE) for p in BANNED_PATTERNS]
    kept, rejected = [], []
    mech_counts: dict[str, int] = {}
    for d in drafts:
        aid, post = d.get("auction_id"), d.get("post") or ""
        headline = (d.get("image_headline") or "").strip()
        if aid not in by_id:
            rejected.append(f"{aid or '?'}: unknown auction_id")
            continue
        # The honesty rule covers every published surface: the caption, the
        # headline burned onto the card, the pinned comment (it carries the
        # disclaimer + link), AND every line rendered into the reel.
        reel_hook = d.get("reel_hook") or {}
        reel_surfaces = "\n".join(filter(None, [
            reel_hook.get("line1"), reel_hook.get("line2"),
            *(d.get("reel_context_lines") or []),
            d.get("engagement_question"), d.get("save_line"),
        ]))
        checked_text = f"{post}\n{headline}\n{d.get('pinned_comment') or ''}\n{reel_surfaces}"
        hits = [p.pattern for p in banned if p.search(checked_text)]
        if hits:
            rejected.append(f"{aid}: banned wording ({', '.join(hits)})")
            continue
        if not HAS_FIGURE.search(post):
            rejected.append(f"{aid}: no concrete figure (fails 'prove it')")
            continue
        if len(post.split()) > MAX_POST_WORDS:
            rejected.append(f"{aid}: over {MAX_POST_WORDS} words")
            continue
        # The pinned comment is required, not optional: it carries the link +
        # the honest disclaimer (and, for research posts, the source URLs).
        # A draft without one is an incomplete post — drop it.
        if not (d.get("pinned_comment") or "").strip():
            rejected.append(f"{aid}: missing pinned_comment (link + disclaimer layer)")
            continue
        hook = extract_hook(post)
        if len(hook) > MAX_HOOK_CHARS:
            rejected.append(
                f"{aid}: hook is {len(hook)} chars (>{MAX_HOOK_CHARS} — dies at the '…more' fold)")
            continue
        lead = re.sub(r"^[^0-9a-zA-Z₹]+", "", hook).lower()
        if lead.startswith(BANNED_OPENERS):
            rejected.append(f"{aid}: throat-clearing opener (fails the stop test)")
            continue
        mech = (d.get("hook_mechanism") or "unspecified").strip().lower()
        if mech_counts.get(mech, 0) >= MAX_PER_MECHANISM:
            rejected.append(
                f"{aid}: hook mechanism '{mech}' already used {MAX_PER_MECHANISM}x in this batch (variety rule)")
            continue
        # A headline destined for the card must fit the card (it becomes the
        # visual hook — see draft_to_island). Over-length headlines are dropped,
        # not truncated, so we never silently cut a hook mid-word on the image.
        if d.get("needs_image") and len(headline) > MAX_HEADLINE_CHARS:
            rejected.append(
                f"{aid}: image_headline is {len(headline)} chars "
                f"(>{MAX_HEADLINE_CHARS} — won't fit the card)")
            continue
        # Reel gates — only when the draft wants a reel. The reel's first
        # frame is the whole game (1-second retention signal), so the budgets
        # are hard and a violation drops the draft's reel copy, not trims it.
        if d.get("needs_reel"):
            reason = _reel_gate_reason(d, reel_hook)
            if reason:
                rejected.append(f"{aid}: {reason}")
                continue
        mech_counts[mech] = mech_counts.get(mech, 0) + 1
        d["hook_mechanism"] = mech
        d["source"] = by_id[aid]  # attach facts for the reviewer
        kept.append(d)
    return kept, rejected


# -------------------------------------------------------- draft → card island
#
# The hook system doesn't stop at the caption: the SAME hook, compressed into
# image_headline, is burned onto the static card as its scroll-stopping
# headline. draft_to_island() maps a validated draft + its grounded source
# fields into the exact #data island a marketing/templates/ template expects,
# so `render_social.py --data <island>` renders a card whose headline IS the
# hook. Every figure comes from the auction's own fields (honesty rule) — the
# only free text is the honesty microcopy and image_headline (already
# banned-word-scanned in validate_drafts).

# angle → (template stem, honesty source_line for that card)
ANGLE_TEMPLATE = {
    "price_drop": ("price-drop-1080x1350",
                   "Both prices from the bank's auction notices — earlier listing vs current re-auction."),
    "closing_soon": ("deal-of-the-day-1080",
                     "Reserve, EMD and date from the bank's auction notice."),
    "cheapest": ("deal-of-the-day-1080",
                 "Reserve, EMD and date from the bank's auction notice."),
}

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _card_date(iso: str | None) -> str:
    """ISO 'auction_start' → card date '24 Jul 2026'; '' if unparseable/missing,
    so the template hides the chip rather than printing a wrong or half date."""
    if not iso:
        return ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(iso))
    if not m:
        return ""
    y, mo, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not 1 <= mo <= 12:
        return ""
    return f"{day} {_MONTHS[mo - 1]} {y}"


def _asset_type(source: dict) -> str:
    types = source.get("property_types") or []
    if types:
        return ", ".join(t for t in types if t)
    return source.get("asset_category") or ""


def draft_to_island(draft: dict) -> tuple[str, dict] | None:
    """Return (template_stem, island_dict) for a validated draft, or None if the
    draft has no image or its angle has no card template. `source` is the
    grounded candidate attached by validate_drafts()."""
    if not draft.get("needs_image"):
        return None
    tpl = ANGLE_TEMPLATE.get(draft.get("angle"))
    if not tpl:
        return None
    template, source_line = tpl
    s = draft.get("source") or {}
    headline = (draft.get("image_headline") or "").strip()
    island: dict = {
        "headline": headline,          # the hook, burned onto the card
        "title": s.get("title") or "",
        "city": s.get("city") or "",
        "asset_type": _asset_type(s),
        "bank": s.get("bank") or "",
        "reserve_price": s.get("reserve_price"),
        "emd": s.get("emd"),           # None → template hides the EMD chip
        "auction_date": _card_date(s.get("auction_start")),
        "source_line": source_line,
    }
    if template == "price-drop-1080x1350":
        island["previous_reserve_price"] = s.get("previous_reserve_price")
    else:
        # deal-of-the-day has a "vs market" row; we hold no comparable, so blank
        # it (the template hides an empty market_hint) — never invent a range.
        island["market_hint"] = ""
    return template, island


def write_card_islands(out_dir: Path, drafts: list[dict]) -> list[dict]:
    """Write one #data island JSON per image-bearing draft into out_dir/cards/.
    Returns a manifest (one row per card) for review.md + drafts.json."""
    manifest: list[dict] = []
    cards_dir = out_dir / "cards"
    for i, d in enumerate(drafts, 1):
        made = draft_to_island(d)
        if not made:
            continue
        template, island = made
        cards_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{i:02d}-{d['auction_id']}.json"
        (cards_dir / fname).write_text(
            json.dumps(island, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest.append({"draft_index": i, "auction_id": d["auction_id"],
                         "template": template, "data": f"cards/{fname}",
                         "headline": island["headline"]})
    return manifest


# -------------------------------------------------------- draft → reel island
#
# The reel is the same hook system on a third surface: the Poster-selected
# reel_hook becomes the video's first frame, the context lines the mid-reel
# curiosity gap, the engagement question the end card. draft_to_reel_island()
# maps a validated draft into the #data island the deal-reel HyperFrames
# template expects; render_reel.py turns it into an MP4. Figures come from
# the auction's own fields; every free-text line was banned-word-scanned in
# validate_drafts before it could get here.

# angle → (reel template stem, honesty line shown on the proof card)
REEL_ANGLE_TEMPLATE = {
    "price_drop": ("deal-reel-1080x1920", ANGLE_TEMPLATE["price_drop"][1]),
    "closing_soon": ("deal-reel-1080x1920", ANGLE_TEMPLATE["closing_soon"][1]),
    "cheapest": ("deal-reel-1080x1920", ANGLE_TEMPLATE["cheapest"][1]),
}


def _days_left(iso: str | None) -> int | None:
    """Days from today to the auction start; None when unparseable or past —
    blanked, never guessed (the template hides the chip)."""
    if not iso:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(iso))
    if not m:
        return None
    try:
        auction = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    delta = (auction - date.today()).days
    return delta if delta >= 0 else None


def draft_to_reel_island(draft: dict) -> tuple[str, dict] | None:
    """Return (template_stem, island_dict) for a reel-bearing validated draft,
    or None when the draft opted out (needs_reel falsy) or the angle has no
    reel template."""
    if not draft.get("needs_reel"):
        return None
    tpl = REEL_ANGLE_TEMPLATE.get(draft.get("angle"))
    if not tpl:
        return None
    template, honesty_line = tpl
    s = draft.get("source") or {}
    hook = draft.get("reel_hook") or {}
    island = {
        "angle": draft.get("angle"),
        "hook": {"line1": hook.get("line1") or "",
                 "line2": hook.get("line2") or ""},
        "context_lines": list(draft.get("reel_context_lines") or [])[:2],
        "money": {
            "previous_reserve_price": s.get("previous_reserve_price"),
            "reserve_price": s.get("reserve_price"),
            "drop_pct": s.get("drop_pct"),
        },
        "facts": {
            "title": s.get("title") or "",
            "city": s.get("city") or "",
            "bank": s.get("bank") or "",
            "asset_type": _asset_type(s),
            "emd": s.get("emd"),
            "auction_date": _card_date(s.get("auction_start")),
            "days_left": _days_left(s.get("auction_start")),
        },
        "endcard": {
            "question": draft.get("engagement_question") or "",
            "save_line": draft.get("save_line") or "",
            "url": "auctionscope.in",
        },
        "honesty_line": honesty_line,
    }
    return template, island


def stats_reel_island(stats: dict, drafts: list[dict]) -> dict | None:
    """The stats-reel island — pure data, no LLM: three count-up rows from
    GET /stats plus 'today's pick' (the model's top validated draft). The
    template header says 'the poster script rewrites the island only'; this
    is that script. Returns None when the numbers or a pick are missing."""
    total = stats.get("total_auctions")
    live = stats.get("upcoming_auctions")
    if not (total and live and drafts):
        return None
    s = drafts[0].get("source") or {}
    if not s.get("reserve_price"):
        return None
    rows = [{"value": live, "label": "live bank auctions tracked"},
            {"value": total, "label": "auctions in the archive"}]
    if stats.get("cities"):
        rows.append({"value": stats["cities"], "label": "cities"})
    return {
        "date_label": _card_date(date.today().isoformat()),
        "stats": rows,
        "pick": {
            "title": s.get("title") or "",
            "loc": " · ".join(filter(None, [s.get("city"), s.get("bank")])),
            "reserve_price": s.get("reserve_price"),
            "auction_date": _card_date(s.get("auction_start")),
        },
    }


def write_reel_islands(out_dir: Path, stats: dict, drafts: list[dict]) -> list[dict]:
    """Write one reel #data island per reel-bearing draft (plus the stats
    reel) into out_dir/reels/. Returns a manifest for review.md/drafts.json —
    each row carries the template stem so render_reel.py needs no lookup."""
    manifest: list[dict] = []
    reels_dir = out_dir / "reels"

    def _write(fname: str, island: dict) -> None:
        reels_dir.mkdir(parents=True, exist_ok=True)
        (reels_dir / fname).write_text(
            json.dumps(island, ensure_ascii=False, indent=2), encoding="utf-8")

    stats_island = stats_reel_island(stats, drafts)
    if stats_island:
        _write("00-stats.json", stats_island)
        manifest.append({"draft_index": 0, "auction_id": "stats",
                         "template": "stats-reel-1080x1920",
                         "data": "reels/00-stats.json"})
    for i, d in enumerate(drafts, 1):
        made = draft_to_reel_island(d)
        if not made:
            continue
        template, island = made
        fname = f"{i:02d}-{d['auction_id']}.json"
        _write(fname, island)
        manifest.append({"draft_index": i, "auction_id": d["auction_id"],
                         "template": template, "data": f"reels/{fname}",
                         "hook": island["hook"]["line1"]})
    return manifest


# ------------------------------------------------------------------ output

def write_outputs(out_root: Path, stats: dict, drafts: list[dict],
                  rejected: list[str], editor_notes: str) -> Path:
    out_dir = out_root / date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    cards = write_card_islands(out_dir, drafts)
    reels = write_reel_islands(out_dir, stats, drafts)
    (out_dir / "drafts.json").write_text(
        json.dumps({"stats": stats, "drafts": drafts, "cards": cards,
                    "reels": reels, "rejected": rejected,
                    "editor_notes": editor_notes},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        f"# Content review — {date.today().isoformat()}",
        "",
        f"{stats.get('upcoming_auctions')} live auctions (of {stats.get('total_auctions')} tracked). "
        f"Data as of {stats.get('last_enriched') or stats.get('generated_at') or 'unknown'}.",
        "",
        "**Staged drafts only — nothing is published.** Review, tweak, and post "
        "manually or via your scheduler. Verify each fact against the linked notice.",
        "",
    ]
    if cards:
        lines += [
            f"**{len(cards)} card image(s) staged** — the hook is burned on as the "
            "headline. Render them with:",
            "```bash",
            *[f"python marketing/render_social.py --template {c['template']} "
              f"--data {(out_dir / c['data'])} --out {out_dir}" for c in cards],
            "```",
            "",
        ]
    if reels:
        lines += [
            f"**{len(reels)} reel(s) staged** — 12s 9:16 videos; the selected "
            "reel hook is the first frame. Render with:",
            "```bash",
            *[f"python marketing/render_reel.py --data {(out_dir / r['data'])} "
              f"--template {r['template']} --out {out_dir}" for r in reels],
            "```",
            "Reels render silent by design — **add trending audio in-app** "
            "(Instagram/TikTok) when publishing; native audio drives reach.",
            "",
        ]
    card_by_index = {c["draft_index"]: c for c in cards}
    reel_by_index = {r["draft_index"]: r for r in reels}
    for i, d in enumerate(drafts, 1):
        s = d["source"]
        price = f"₹{s['reserve_lakhs']}L"
        if d["angle"] == "price_drop":
            price = f"₹{s['previous_reserve_lakhs']}L → ₹{s['reserve_lakhs']}L (−{s['drop_pct']}%)"
        lines += [
            f"## Draft {i} — {d['angle']} — {s.get('city') or '?'}",
            f"*{s['title']}* · {price} · ends {s.get('auction_start') or '?'} · "
            f"[source notice]({s.get('url')}) · `{d['auction_id']}`",
            "",
            d["post"],
            "",
            f"tags: {' '.join('#' + h for h in d.get('hashtags', []))} · "
            f"image: {'yes — ' + d.get('image_headline', '') if d.get('needs_image') else 'no'}",
            "",
        ]
        alts = [a for a in (d.get("hook_alternatives") or [])
                if isinstance(a, str) and a.strip()]
        lines.append(f"hook: `{d.get('hook_mechanism', 'unspecified')}`"
                     + (" · alternatives:" if alts else ""))
        lines += [f"- {a}" for a in alts[:2]]
        card = card_by_index.get(i)
        if card:
            lines.append(f"card: `{card['template']}` · headline burned on: "
                         f"*{card['headline'] or '(none — card shows the property title)'}*")
        reel = reel_by_index.get(i)
        if reel:
            rh = d.get("reel_hook") or {}
            lines.append(
                f"reel: `{reel['template']}` · first frame: *{rh.get('line1', '')} / "
                f"{rh.get('line2', '')}* · end card: *{d.get('engagement_question', '')}*")
        lines.append("")
        if d.get("pinned_comment"):
            lines += ["**pinned comment:**", "> " + d["pinned_comment"].replace("\n", "\n> "), ""]
        meta = [f"{k}: {d[k]}" for k in ("video_title", "location_tag", "alt_text") if d.get(k)]
        if meta:
            lines += ["`" + "` · `".join(meta) + "`", ""]
    if rejected:
        lines += ["## Dropped by validation", *[f"- {r}" for r in rejected], ""]
    if editor_notes:
        lines += ["## Editor notes", editor_notes, ""]
    (out_dir / "review.md").write_text("\n".join(lines), encoding="utf-8")
    return out_dir


# ------------------------------------------------------------ pipeline steps
#
# The Poster runs as three swappable stages so the "brain" can be either
# Claude Code on the founder's Max subscription (default in CI) or a direct
# OpenRouter call (fallback):
#
#   --prepare              fetch data, write <work>/candidates.json + prompt.txt
#   (an engine writes <work>/response.txt from prompt.txt)
#   --generate             the OpenRouter engine: prompt.txt -> response.txt
#   --finalize             parse + validate response.txt, stage the outputs
#
# Running with no stage flag does prepare -> generate -> finalize in-process
# (the original single-shot OpenRouter behavior, handy locally).

def load_recent_performance(out_root: Path | None = None) -> str:
    """The Part-7 feedback hook (copy-playbook.md): surface Agent B's latest
    report to the drafting prompt so mechanism/angle choice is biased by what
    actually performed. Pre-traction this is the qualitative loop — silently
    absent when no report exists. Best-effort by design: a malformed report
    must never block drafting."""
    root = out_root if out_root is not None else Path("marketing/outputs")
    try:
        reports = sorted(root.glob("*/report.json"))
        if not reports:
            return ""
        data = json.loads(reports[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    parts: list[str] = []
    computed = data.get("computed") or data
    for key in ("by_angle", "by_format"):
        table = computed.get(key)
        if isinstance(table, dict) and table:
            rows = ", ".join(f"{k}: {v}" for k, v in list(table.items())[:6])
            parts.append(f"{key}: {rows}")
    actions = data.get("next_week") or data.get("actions")
    if isinstance(actions, list) and actions:
        parts.append("next_week: " + " | ".join(str(a) for a in actions[:4]))
    return "\n".join(parts)


def step_prepare(work_dir: Path, max_drafts: int) -> int:
    api_base = os.environ.get("API_BASE", API_BASE_DEFAULT)
    stats, candidates = fetch_pool(api_base)
    n_drops = sum(1 for c in candidates if c["angle"] == "price_drop")
    print(f"pool: {len(candidates)} candidates ({n_drops} price drops) · "
          f"{stats.get('upcoming_auctions')} live auctions")
    if not candidates:
        print("No live candidates — nothing to draft (is the data fresh?).")
        return 1
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "candidates.json").write_text(
        json.dumps({"stats": stats, "candidates": candidates,
                    "max_drafts": max_drafts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    perf = load_recent_performance()
    if perf:
        print("recent report.json found — mechanism bias injected into the prompt")
    (work_dir / "prompt.txt").write_text(
        build_prompt(stats, candidates, max_drafts, recent_performance=perf),
        encoding="utf-8")
    print(f"prepared {work_dir}/candidates.json + prompt.txt")
    return 0


def step_generate(work_dir: Path) -> int:
    """The OpenRouter engine (fallback). Claude-Max runs replace this stage
    with Claude Code writing response.txt from prompt.txt directly."""
    api_key = resolve_api_key()
    if not api_key:
        print("OPENROUTER_CHAT_API_KEY (or OPENROUTER_API_KEY) is required "
              "for --generate.", file=sys.stderr)
        return 2
    prompt = (work_dir / "prompt.txt").read_text(encoding="utf-8")
    model = os.environ.get("OPENROUTER_MODEL", MODEL_DEFAULT)
    (work_dir / "response.txt").write_text(
        call_llm(prompt, api_key, model), encoding="utf-8")
    print(f"wrote {work_dir}/response.txt via OpenRouter ({model})")
    return 0


def step_finalize(work_dir: Path, response_path: Path | None = None) -> int:
    data = json.loads((work_dir / "candidates.json").read_text(encoding="utf-8"))
    raw = (response_path or work_dir / "response.txt").read_text(encoding="utf-8")
    parsed = parse_llm_json(raw)
    drafts, rejected = validate_drafts(parsed.get("drafts", []), data["candidates"])
    out_root = Path(os.environ.get("POSTER_OUT_DIR", "marketing/outputs"))
    out_dir = write_outputs(out_root, data["stats"], drafts, rejected,
                            parsed.get("editor_notes", ""))
    print(f"wrote {len(drafts)} drafts ({len(rejected)} rejected) → {out_dir}/")
    return 0 if drafts else 1


# -------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch + shape data and print a summary; no LLM call")
    parser.add_argument("--max-drafts", type=int, default=5)
    parser.add_argument("--prepare", action="store_true",
                        help="stage 1: fetch data, write candidates.json + prompt.txt")
    parser.add_argument("--generate", action="store_true",
                        help="stage 2 (OpenRouter engine): prompt.txt -> response.txt")
    parser.add_argument("--finalize", action="store_true",
                        help="stage 3: validate response.txt and stage the outputs")
    parser.add_argument("--response", type=Path, default=None,
                        help="path to the engine's response (default: <work>/response.txt)")
    parser.add_argument("--work-dir", type=Path,
                        default=Path(os.environ.get("POSTER_WORK_DIR", ".poster_work")))
    args = parser.parse_args(argv)

    if os.environ.get("AGENTS_ENABLED", "true").lower() == "false":
        print("AGENTS_ENABLED=false — kill switch on, exiting without doing anything.")
        return 0

    if args.prepare:
        return step_prepare(args.work_dir, args.max_drafts)
    if args.generate:
        return step_generate(args.work_dir)
    if args.finalize:
        return step_finalize(args.work_dir, args.response)

    # No stage flag: original single-shot behavior (in-process, OpenRouter).
    api_base = os.environ.get("API_BASE", API_BASE_DEFAULT)
    stats, candidates = fetch_pool(api_base)
    n_drops = sum(1 for c in candidates if c["angle"] == "price_drop")
    print(f"pool: {len(candidates)} candidates ({n_drops} price drops) · "
          f"{stats.get('upcoming_auctions')} live auctions")

    if not candidates:
        print("No live candidates — nothing to draft (is the data fresh?).")
        return 1

    if args.dry_run:
        print(json.dumps(candidates[:5], ensure_ascii=False, indent=2))
        print("dry run — stopping before the LLM call.")
        return 0

    api_key = resolve_api_key()
    if not api_key:
        print("OPENROUTER_CHAT_API_KEY (or OPENROUTER_API_KEY) is required "
              "(or use --dry-run).", file=sys.stderr)
        return 2

    model = os.environ.get("OPENROUTER_MODEL", MODEL_DEFAULT)
    prompt = build_prompt(stats, candidates, args.max_drafts)
    raw = call_llm(prompt, api_key, model)
    try:
        parsed = parse_llm_json(raw)
    except (ValueError, json.JSONDecodeError):
        print("model reply was not valid JSON — retrying once")
        parsed = parse_llm_json(call_llm(prompt, api_key, model))

    drafts, rejected = validate_drafts(parsed.get("drafts", []), candidates)
    out_root = Path(os.environ.get("POSTER_OUT_DIR", "marketing/outputs"))
    out_dir = write_outputs(out_root, stats, drafts, rejected,
                            parsed.get("editor_notes", ""))
    print(f"wrote {len(drafts)} drafts ({len(rejected)} rejected) → {out_dir}/")
    return 0 if drafts else 1


if __name__ == "__main__":
    raise SystemExit(main())
