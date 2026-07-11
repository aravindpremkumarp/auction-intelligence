"""Agent A — "The Poster": draft social posts from live auction data.

Runs AFTER each data refresh (manual workflow_dispatch — see
.github/workflows/content-poster.yml), never on a fixed clock, so drafts
always reflect fresh data. Spec: docs/marketing/content-agents.md.

Tier-1 only: this module DRAFTS and STAGES. It never publishes anywhere.
Output lands in marketing/outputs/YYYY-MM-DD/ for human review.

Env:
    API_BASE            auction API base (default: production Render URL)
    OPENROUTER_API_KEY  required unless --dry-run
    OPENROUTER_MODEL    chat model (default: deepseek/deepseek-v4-flash)
    AGENTS_ENABLED      kill switch — anything but "false" means enabled
    POSTER_OUT_DIR      output root (default: marketing/outputs)

Usage:
    python -m marketing_agents.poster            # full run (needs API key)
    python -m marketing_agents.poster --dry-run  # fetch + shape only, no LLM
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

SITE SNAPSHOT: {stats.get("upcoming_auctions")} live auctions of {stats.get("total_auctions")} tracked (as of {stats.get("generated_at")}).

CANDIDATE AUCTIONS (JSON):
{json.dumps(candidates, ensure_ascii=False, indent=1)}

TASK: pick the {max_drafts} most interesting candidates (prefer price_drop angles,
then closing_soon, then cheapest; vary cities) and write one post draft each.
A good draft has: a concrete hook (the number does the work), one idea, and
honest framing ("reserve price", "bank auction", "ends <date>").

OUTPUT — ONLY valid JSON, no prose, no code fences:
{{
  "drafts": [
    {{
      "auction_id": string,           // must match a candidate exactly
      "angle": string,                // price_drop | closing_soon | cheapest
      "post": string,                 // the cross-platform caption
      "hashtags": [string],           // 3-5, no # prefix
      "needs_image": boolean,
      "image_headline": string        // <=8 words, for the deal-card template
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
            "max_tokens": 3000,
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

def validate_drafts(drafts: list[dict], candidates: list[dict]) -> tuple[list[dict], list[str]]:
    """Enforce grounding + the honesty rule. Violators are dropped, not fixed."""
    by_id = {c["auction_id"]: c for c in candidates}
    banned = [re.compile(p, re.IGNORECASE) for p in BANNED_PATTERNS]
    kept, rejected = [], []
    for d in drafts:
        aid, post = d.get("auction_id"), d.get("post") or ""
        if aid not in by_id:
            rejected.append(f"{aid or '?'}: unknown auction_id")
            continue
        hits = [p.pattern for p in banned if p.search(post)]
        if hits:
            rejected.append(f"{aid}: banned wording ({', '.join(hits)})")
            continue
        if len(post.split()) > MAX_POST_WORDS:
            rejected.append(f"{aid}: over {MAX_POST_WORDS} words")
            continue
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
        f"Data as of {stats.get('last_enriched')}.",
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
    if rejected:
        lines += ["## Dropped by validation", *[f"- {r}" for r in rejected], ""]
    if editor_notes:
        lines += ["## Editor notes", editor_notes, ""]
    (out_dir / "review.md").write_text("\n".join(lines), encoding="utf-8")
    return out_dir


# -------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch + shape data and print a summary; no LLM call")
    parser.add_argument("--max-drafts", type=int, default=5)
    args = parser.parse_args(argv)

    if os.environ.get("AGENTS_ENABLED", "true").lower() == "false":
        print("AGENTS_ENABLED=false — kill switch on, exiting without doing anything.")
        return 0

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

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY is required (or use --dry-run).", file=sys.stderr)
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
