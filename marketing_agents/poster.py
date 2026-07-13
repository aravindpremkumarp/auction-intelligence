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
BANNED_OPENERS = (
    "did you know", "attention", "imagine", "are you looking",
    "introducing", "we're excited", "we are excited",
    "don't miss", "dont miss", "hurry", "last chance",
)
HOOK_MECHANISMS = ("contrast", "question", "mistake", "hidden",
                   "myth", "callout", "countdown", "process")
MAX_PER_MECHANISM = 2  # variety rule: a batch may not lean on one mechanism


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


def build_prompt(stats: dict, candidates: list[dict], max_drafts: int) -> str:
    return f"""You are the social content drafter for AuctionScope (auctionscope.in) —
an AI research assistant for Indian bank-auction (SARFAESI) property in Tamil Nadu.

BRAND CONTEXT (follow the voice; respect words-to-avoid strictly):
{load_brand_context()}

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

OUTPUT — ONLY valid JSON, no prose, no code fences:
{{
  "drafts": [
    {{
      "auction_id": string,           // must match a candidate exactly
      "angle": string,                // price_drop | closing_soon | cheapest
      "hook_mechanism": string,       // contrast | question | mistake | hidden | myth | callout | countdown | process
      "hook_alternatives": [string],  // the 2 runner-up hooks (grounded, honest)
      "post": string,                 // hook on line 1 (<=100 chars), blank line, body
      "hashtags": [string],           // 3-5, no # prefix
      "needs_image": boolean,
      "image_headline": string        // <=8 words: the hook compressed, same mechanism
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


def validate_drafts(drafts: list[dict], candidates: list[dict]) -> tuple[list[dict], list[str]]:
    """Enforce grounding + the honesty rule. Violators are dropped, not fixed."""
    by_id = {c["auction_id"]: c for c in candidates}
    banned = [re.compile(p, re.IGNORECASE) for p in BANNED_PATTERNS]
    kept, rejected = [], []
    mech_counts: dict[str, int] = {}
    for d in drafts:
        aid, post = d.get("auction_id"), d.get("post") or ""
        if aid not in by_id:
            rejected.append(f"{aid or '?'}: unknown auction_id")
            continue
        hits = [p.pattern for p in banned if p.search(post)]
        if hits:
            rejected.append(f"{aid}: banned wording ({', '.join(hits)})")
            continue
        if not HAS_FIGURE.search(post):
            rejected.append(f"{aid}: no concrete figure (fails 'prove it')")
            continue
        if len(post.split()) > MAX_POST_WORDS:
            rejected.append(f"{aid}: over {MAX_POST_WORDS} words")
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
        mech_counts[mech] = mech_counts.get(mech, 0) + 1
        d["hook_mechanism"] = mech
        d["source"] = by_id[aid]  # attach facts for the reviewer
        kept.append(d)
    return kept, rejected


# ------------------------------------------------------------------ output

def write_outputs(out_root: Path, stats: dict, drafts: list[dict],
                  rejected: list[str], editor_notes: str) -> Path:
    out_dir = out_root / date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "drafts.json").write_text(
        json.dumps({"stats": stats, "drafts": drafts, "rejected": rejected,
                    "editor_notes": editor_notes}, ensure_ascii=False, indent=2),
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
        lines.append("")
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
    (work_dir / "prompt.txt").write_text(
        build_prompt(stats, candidates, max_drafts), encoding="utf-8")
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
