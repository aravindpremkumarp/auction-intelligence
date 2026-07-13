"""Agent B — "The Reporter": weekly social performance report from post metrics.

The counterpart to `marketing_agents/poster.py` (Agent A). Spec:
docs/marketing/content-agents.md ("Agent B — The Reporter"). Where the Poster
turns auction data into post drafts, the Reporter turns *post metrics* into a
one-page report: what worked, what didn't (with a hypothesis), the week's
pattern, and 3-4 concrete next-week actions.

Read-only on the outside world — it reads a CSV you export from your platforms
(plus GET /stats for a site snapshot) and writes a staged report. Nothing is
published. Safe to run unattended (Tier 1, per content-agents.md guardrails).

The analysis method — reach/engagement/conversion tiers, own-baseline
benchmarking (never platform vanity averages), every observation ends in an
action — is distilled from the blacktwist `performance-analyzer-sms` /
`content-pattern-analyzer-sms` / `optimization-advisor-sms` skills into
docs/marketing/analytics-playbook.md, the same way copy-playbook.md distills
the writing skills for the Poster.

GROUNDING (the analytics analog of the Poster's honesty rule): every metric —
engagement rate, baseline, trend, per-angle number — is computed HERE in
Python from the CSV. The LLM only *interprets* the computed table; it is told
never to invent a number, and the finalize step drops a report that cites a
post_id the CSV doesn't contain.

Metrics CSV (header row; extra columns ignored, missing optional cols = 0):
    post_id,date,platform,angle,format,impressions,likes,comments,reposts,saves,link_clicks,profile_visits
  - angle:  price_drop | closing_soon | cheapest | evaluate | educate  (our pillars)
  - format: static | carousel | reel
  - The minimum useful analysis needs impressions+likes+comments for >=5 posts.

Env:
    API_BASE                 auction API base (default: production Render URL)
    OPENROUTER_CHAT_API_KEY  preferred key; falls back to OPENROUTER_API_KEY
                             (mirrors poster.py / api/main.py). OpenRouter engine only.
    OPENROUTER_MODEL         chat model (default: deepseek/deepseek-v4-flash)
    AGENTS_ENABLED           kill switch — anything but "false" means enabled
    REPORTER_OUT_DIR         output root (default: marketing/outputs)
    REPORTER_WORK_DIR        staged-run scratch dir (default: .reporter_work)

Usage:
    python -m marketing_agents.reporter --metrics posts.csv --dry-run   # compute + print stats, no LLM
    python -m marketing_agents.reporter --metrics posts.csv --prepare   # stage 1: stats -> prompt.txt
    python -m marketing_agents.reporter --generate                      # stage 2 (OpenRouter fallback)
    python -m marketing_agents.reporter --finalize                      # stage 3: validate + stage report
    python -m marketing_agents.reporter --metrics posts.csv             # single-shot (OpenRouter)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from statistics import mean, median

import httpx

API_BASE_DEFAULT = "https://auction-api-w68b.onrender.com"
MODEL_DEFAULT = "deepseek/deepseek-v4-flash"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
PRODUCT_MARKETING_PATH = Path(".agents/product-marketing.md")

MIN_POSTS = 5  # the analyzer skill's floor: fewer than this isn't a signal

# Metric columns we sum for engagement. Reach/conversion tiers are reported
# separately (see docs/marketing/analytics-playbook.md).
ENGAGEMENT_COLS = ("likes", "comments", "reposts", "saves")
NUMERIC_COLS = (
    "impressions", "likes", "comments", "reposts", "saves",
    "link_clicks", "profile_visits",
)

# A report is interpretation, not promotion — reuse the honesty rule so the
# Reporter never launders hype into a "growth" claim it can't stand behind.
BANNED_PATTERNS = [
    r"\bguaranteed?\b", r"\bviral\b", r"\bexplosive\b", r"\bskyrocket",
    r"\b10x\b", r"\bhack\b", r"\bsecret\b",
]


# ---------------------------------------------------------------- data layer

def _num(value: str | None) -> float:
    """Parse a CSV cell to a number; blanks/junk -> 0 (metrics are non-negative)."""
    if value is None:
        return 0.0
    s = str(value).strip().replace(",", "")
    if not s:
        return 0.0
    try:
        return max(0.0, float(s))
    except ValueError:
        return 0.0


def load_metrics(csv_path: Path) -> list[dict]:
    """Read the metrics CSV into normalized rows; one row per post."""
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows = []
        for i, raw in enumerate(reader, 1):
            row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
            post = {
                "post_id": row.get("post_id") or row.get("post") or f"post-{i}",
                "date": row.get("date", ""),
                "platform": row.get("platform", ""),
                "angle": (row.get("angle") or "").lower(),
                "format": (row.get("format") or "").lower(),
            }
            for col in NUMERIC_COLS:
                post[col] = _num(row.get(col))
            rows.append(post)
    return rows


def engagement_rate(post: dict) -> float:
    """(likes+comments+reposts+saves) / impressions * 100. 0 impressions -> 0."""
    imp = post["impressions"]
    if imp <= 0:
        return 0.0
    return round(sum(post[c] for c in ENGAGEMENT_COLS) / imp * 100, 2)


def _breakdown(posts: list[dict], key: str) -> list[dict]:
    """Average ER + impressions grouped by a dimension (angle/format/platform)."""
    groups: dict[str, list[dict]] = {}
    for p in posts:
        label = p.get(key) or "unlabelled"
        groups.setdefault(label, []).append(p)
    out = []
    for label, rows in groups.items():
        out.append({
            "label": label,
            "posts": len(rows),
            "avg_er": round(mean(engagement_rate(r) for r in rows), 2),
            "avg_impressions": round(mean(r["impressions"] for r in rows)),
        })
    return sorted(out, key=lambda g: g["avg_er"], reverse=True)


def compute_report(posts: list[dict]) -> dict:
    """Deterministic analysis: baselines, top/bottom, trend, breakdowns.

    Every number the LLM later interprets is computed here, so the report can
    never cite a metric that isn't in the data.
    """
    for p in posts:
        p["er"] = engagement_rate(p)
    ranked = sorted(posts, key=lambda p: p["er"], reverse=True)

    ers = [p["er"] for p in posts]
    baseline = {
        "avg_er": round(mean(ers), 2),
        "median_er": round(median(ers), 2),
        "avg_impressions": round(mean(p["impressions"] for p in posts)),
        "avg_comments": round(mean(p["comments"] for p in posts), 1),
        "total_link_clicks": round(sum(p["link_clicks"] for p in posts)),
        "total_profile_visits": round(sum(p["profile_visits"] for p in posts)),
    }

    # Trend: split the window in half chronologically (fall back to input order
    # when dates are missing), compare average ER + impressions across halves.
    chron = sorted(posts, key=lambda p: (p["date"] or "", posts.index(p)))
    mid = len(chron) // 2
    first, second = chron[:mid], chron[mid:]
    trend = None
    if first and second:
        d_er = round(mean(p["er"] for p in second) - mean(p["er"] for p in first), 2)
        d_imp = round(mean(p["impressions"] for p in second)
                      - mean(p["impressions"] for p in first))
        trend = {
            "er_delta": d_er,
            "impressions_delta": d_imp,
            "er_direction": "up" if d_er > 0.15 else "down" if d_er < -0.15 else "flat",
        }

    def slim(p: dict) -> dict:
        return {
            "post_id": p["post_id"], "date": p["date"], "platform": p["platform"],
            "angle": p["angle"], "format": p["format"], "er": p["er"],
            "impressions": round(p["impressions"]), "likes": round(p["likes"]),
            "comments": round(p["comments"]), "reposts": round(p["reposts"]),
            "saves": round(p["saves"]), "link_clicks": round(p["link_clicks"]),
        }

    n_top = min(5, max(3, len(posts) // 3))
    return {
        "posts_analyzed": len(posts),
        "baseline": baseline,
        "trend": trend,
        "top_performers": [slim(p) for p in ranked[:n_top]],
        "bottom_performers": [slim(p) for p in ranked[-n_top:][::-1]],
        "by_angle": _breakdown(posts, "angle"),
        "by_format": _breakdown(posts, "format"),
        "by_platform": _breakdown(posts, "platform"),
        "valid_post_ids": [p["post_id"] for p in posts],
    }


def fetch_stats(api_base: str) -> dict:
    """Site snapshot for context (best effort — the report still works offline)."""
    try:
        with httpx.Client(base_url=api_base, timeout=30) as client:
            resp = client.get("/stats")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001 — report must not die on a flaky API
        return {"_stats_error": str(exc)}


def resolve_api_key(env: dict | None = None) -> str | None:
    env = env if env is not None else os.environ
    return env.get("OPENROUTER_CHAT_API_KEY") or env.get("OPENROUTER_API_KEY")


# ------------------------------------------------------------- prompt layer

def load_brand_context() -> str:
    """Brand voice from product-marketing.md — the single source of truth.

    (The installed *-sms skills expect .agents/social-media-context-sms.md; here
    the brand hub is product-marketing.md, so the auctionscope-native Reporter
    reads that instead — same relationship copy-playbook.md has to the writing
    skills.)
    """
    try:
        text = PRODUCT_MARKETING_PATH.read_text(encoding="utf-8")
    except OSError:
        return "Tone: understated, plain-spoken, lowercase, calm, no hype."
    sections = []
    for heading in ("## Brand Voice", "## Target Audience"):
        idx = text.find(heading)
        if idx != -1:
            end = text.find("\n## ", idx + 1)
            sections.append(text[idx : end if end != -1 else len(text)])
    return "\n\n".join(sections) or text[:2000]


def build_prompt(report: dict, stats: dict) -> str:
    snapshot = "unavailable"
    if "_stats_error" not in stats:
        snapshot = (f"{stats.get('upcoming_auctions')} live auctions of "
                    f"{stats.get('total_auctions')} tracked")
    return f"""You are the weekly marketing analyst for AuctionScope (auctionscope.in) —
an AI research assistant for Indian bank-auction (SARFAESI) property in Tamil Nadu.

BRAND CONTEXT (write in this voice — understated, lowercase, plain):
{load_brand_context()}

METHOD (docs/marketing/analytics-playbook.md):
- Benchmark every post against OUR OWN baseline below, never industry averages.
- "angle" maps to our content pillars (price_drop / closing_soon / cheapest /
  evaluate / educate). "format" is static / carousel / reel.
- Engagement rate is the currency, not raw likes: a post at 8% ER from 500
  impressions beats one at 2% from 10,000.
- EVERY observation must end in a concrete action. Not "post more" but
  "run 3 price-drop posts Tue-Thu".

HARD RULES:
- Use ONLY the computed numbers below. NEVER invent or estimate a metric,
  follower count, or percentage that isn't in the data.
- Reference posts only by a post_id that appears in the data.
- No hype words: guaranteed, viral, explosive, skyrocket, 10x, hack, secret.
- We report honestly: if the sample is small or a trend is weak, say so.

SITE SNAPSHOT: {snapshot}.

COMPUTED ANALYSIS (JSON — these numbers are ground truth):
{json.dumps(report, ensure_ascii=False, indent=1)}

TASK: write a one-page weekly report with these sections:
1. Headline — one sentence: the single most important thing this week.
2. What worked — the top performers, and WHY (hook/angle/format/timing), each
   tied to how far above baseline it was.
3. What didn't — the bottom performers, with a HYPOTHESIS for each (weak hook?
   wrong angle? off-platform?). Frame as learnings, not failures.
4. The pattern — the clearest signal across angle/format/platform breakdowns
   and the trend (is ER rising or falling? what does it mean?).
5. Next week — 3-4 ranked, concrete actions, each tied to a finding above.
6. Flag — one of green / yellow / red, with a one-line reason.

OUTPUT — ONLY valid JSON, no prose, no code fences:
{{
  "headline": string,
  "flag": "green" | "yellow" | "red",
  "flag_reason": string,
  "what_worked": [ {{ "post_id": string, "why": string }} ],
  "what_didnt":  [ {{ "post_id": string, "hypothesis": string }} ],
  "pattern": string,
  "next_week": [ string ],        // 3-4 ranked concrete actions
  "caveats": string               // small-sample / missing-data honesty
}}"""


def call_llm(prompt: str, api_key: str, model: str) -> str:
    resp = httpx.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "max_tokens": 3000,
            "temperature": 0.5,  # analysis wants less wander than drafting
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def parse_llm_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model reply")
    return json.loads(text[start : end + 1])


# --------------------------------------------------------------- validation

def validate_report(parsed: dict, report: dict) -> tuple[dict, list[str]]:
    """Enforce grounding + the honesty rule. Bad references are dropped."""
    valid_ids = set(report["valid_post_ids"])
    banned = [re.compile(p, re.IGNORECASE) for p in BANNED_PATTERNS]
    warnings: list[str] = []

    def clean_refs(items: list[dict], id_key: str, text_key: str) -> list[dict]:
        kept = []
        for it in items or []:
            pid = it.get("post_id")
            if pid not in valid_ids:
                warnings.append(f"dropped {id_key} ref to unknown post_id {pid!r}")
                continue
            hits = [b.pattern for b in banned if b.search(it.get(text_key, ""))]
            if hits:
                warnings.append(f"{pid}: hype wording in {text_key} ({', '.join(hits)})")
                continue
            kept.append(it)
        return kept

    parsed["what_worked"] = clean_refs(parsed.get("what_worked", []), "what_worked", "why")
    parsed["what_didnt"] = clean_refs(parsed.get("what_didnt", []), "what_didnt", "hypothesis")

    # Honesty rule on free-text fields.
    for field in ("headline", "pattern", "caveats"):
        hits = [b.pattern for b in banned if b.search(parsed.get(field, ""))]
        if hits:
            warnings.append(f"{field}: hype wording ({', '.join(hits)})")
    # Agent B rule: the report must end in actions.
    if not parsed.get("next_week"):
        warnings.append("no next_week actions — every report must end in actions")
    if parsed.get("flag") not in ("green", "yellow", "red"):
        parsed["flag"] = "yellow"
        warnings.append("missing/invalid flag — defaulted to yellow")
    return parsed, warnings


# ------------------------------------------------------------------ output

def _fmt_post_line(report: dict, pid: str) -> str:
    for p in report["top_performers"] + report["bottom_performers"]:
        if p["post_id"] == pid:
            tag = f"{p['angle'] or '—'}/{p['format'] or '—'}"
            return (f"`{pid}` ({tag}, {p['platform'] or '—'}) — "
                    f"**{p['er']}% ER** · {p['impressions']:,} impr · "
                    f"{p['comments']} comments")
    return f"`{pid}`"


def render_markdown(report: dict, parsed: dict, stats: dict, warnings: list[str]) -> str:
    b = report["baseline"]
    trend = report["trend"]
    flag_face = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(parsed["flag"], "🟡")
    lines = [
        f"# Weekly marketing report — {date.today().isoformat()}",
        "",
        f"{flag_face} **{parsed['flag'].upper()}** — {parsed.get('flag_reason', '')}",
        "",
        f"**{parsed.get('headline', '')}**",
        "",
        "**Read-only, staged report — no numbers were invented; every figure is "
        "computed from your metrics CSV.**",
        "",
        "## The numbers",
        f"- Posts analyzed: **{report['posts_analyzed']}**",
        f"- Your baseline engagement rate: **{b['avg_er']}%** (median {b['median_er']}%)",
        f"- Avg impressions/post: **{b['avg_impressions']:,}** · "
        f"avg comments/post: **{b['avg_comments']}**",
        f"- Link clicks: **{b['total_link_clicks']}** · "
        f"profile visits: **{b['total_profile_visits']}**",
    ]
    if trend:
        arrow = {"up": "↑", "down": "↓", "flat": "→"}[trend["er_direction"]]
        lines.append(
            f"- Trend (2nd half vs 1st): ER {arrow} {trend['er_delta']:+}pp · "
            f"impressions {trend['impressions_delta']:+,}")
    lines += ["", "## What worked"]
    for it in parsed.get("what_worked", []):
        lines += [f"- {_fmt_post_line(report, it['post_id'])}", f"  - {it['why']}"]
    lines += ["", "## What didn't"]
    for it in parsed.get("what_didnt", []):
        lines += [f"- {_fmt_post_line(report, it['post_id'])}",
                  f"  - hypothesis: {it['hypothesis']}"]
    lines += ["", "## The pattern", parsed.get("pattern", ""), ""]

    # Deterministic breakdown tables — the ground truth behind the pattern.
    for title, key in (("By angle (pillar)", "by_angle"), ("By format", "by_format")):
        rows = [g for g in report[key] if g["label"] != "unlabelled"]
        if not rows:
            continue
        lines += [f"### {title}", "", "| Segment | Posts | Avg ER | Avg impressions |",
                  "|---|--:|--:|--:|"]
        lines += [f"| {g['label']} | {g['posts']} | {g['avg_er']}% | "
                  f"{g['avg_impressions']:,} |" for g in rows]
        lines.append("")
    lines += ["## Next week"]
    lines += [f"{i}. {a}" for i, a in enumerate(parsed.get("next_week", []), 1)]
    if parsed.get("caveats"):
        lines += ["", "## Caveats", parsed["caveats"]]
    if warnings:
        lines += ["", "## Validation notes", *[f"- {w}" for w in warnings]]
    return "\n".join(lines) + "\n"


def write_outputs(out_root: Path, report: dict, parsed: dict, stats: dict,
                  warnings: list[str]) -> Path:
    out_dir = out_root / date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps({"computed": report, "report": parsed, "stats": stats,
                    "warnings": warnings}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "report.md").write_text(
        render_markdown(report, parsed, stats, warnings), encoding="utf-8")
    return out_dir


# ------------------------------------------------------------ pipeline steps
#
# Same three swappable stages as the Poster, so the "brain" can be Claude Code
# on the Max subscription (default) or OpenRouter (fallback):
#   --prepare   load CSV, compute stats, write <work>/report_input.json + prompt.txt
#   (an engine writes <work>/response.txt from prompt.txt)
#   --generate  the OpenRouter engine: prompt.txt -> response.txt
#   --finalize  parse + validate response.txt, stage report.md/.json

def _load_and_compute(csv_path: Path) -> tuple[dict, dict] | None:
    if not csv_path.exists():
        print(f"metrics CSV not found: {csv_path}", file=sys.stderr)
        return None
    posts = load_metrics(csv_path)
    if len(posts) < MIN_POSTS:
        print(f"only {len(posts)} posts — need at least {MIN_POSTS} for a useful "
              f"analysis (blacktwist analyzer floor). Add more rows.", file=sys.stderr)
        return None
    stats = fetch_stats(os.environ.get("API_BASE", API_BASE_DEFAULT))
    return compute_report(posts), stats


def step_prepare(work_dir: Path, csv_path: Path) -> int:
    computed = _load_and_compute(csv_path)
    if computed is None:
        return 1
    report, stats = computed
    print(f"analyzed {report['posts_analyzed']} posts · "
          f"baseline ER {report['baseline']['avg_er']}%")
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "report_input.json").write_text(
        json.dumps({"report": report, "stats": stats}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (work_dir / "prompt.txt").write_text(build_prompt(report, stats), encoding="utf-8")
    print(f"prepared {work_dir}/report_input.json + prompt.txt")
    return 0


def step_generate(work_dir: Path) -> int:
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
    data = json.loads((work_dir / "report_input.json").read_text(encoding="utf-8"))
    raw = (response_path or work_dir / "response.txt").read_text(encoding="utf-8")
    parsed = parse_llm_json(raw)
    parsed, warnings = validate_report(parsed, data["report"])
    out_root = Path(os.environ.get("REPORTER_OUT_DIR", "marketing/outputs"))
    out_dir = write_outputs(out_root, data["report"], parsed, data["stats"], warnings)
    print(f"wrote report ({len(warnings)} validation notes) → {out_dir}/")
    return 0


# -------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", type=Path,
                        help="CSV of post metrics (required for --prepare / dry-run / single-shot)")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute and print the analysis; no LLM call")
    parser.add_argument("--prepare", action="store_true",
                        help="stage 1: compute stats, write report_input.json + prompt.txt")
    parser.add_argument("--generate", action="store_true",
                        help="stage 2 (OpenRouter engine): prompt.txt -> response.txt")
    parser.add_argument("--finalize", action="store_true",
                        help="stage 3: validate response.txt and stage the report")
    parser.add_argument("--response", type=Path, default=None,
                        help="path to the engine's response (default: <work>/response.txt)")
    parser.add_argument("--work-dir", type=Path,
                        default=Path(os.environ.get("REPORTER_WORK_DIR", ".reporter_work")))
    args = parser.parse_args(argv)

    if os.environ.get("AGENTS_ENABLED", "true").lower() == "false":
        print("AGENTS_ENABLED=false — kill switch on, exiting without doing anything.")
        return 0

    if args.prepare:
        if not args.metrics:
            print("--prepare needs --metrics <csv>", file=sys.stderr)
            return 2
        return step_prepare(args.work_dir, args.metrics)
    if args.generate:
        return step_generate(args.work_dir)
    if args.finalize:
        return step_finalize(args.work_dir, args.response)

    # No stage flag: single-shot (compute -> OpenRouter -> validate -> stage).
    if not args.metrics:
        print("--metrics <csv> is required (or use a staged flag)", file=sys.stderr)
        return 2
    computed = _load_and_compute(args.metrics)
    if computed is None:
        return 1
    report, stats = computed
    print(f"analyzed {report['posts_analyzed']} posts · "
          f"baseline ER {report['baseline']['avg_er']}%")

    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("dry run — stopping before the LLM call.")
        return 0

    api_key = resolve_api_key()
    if not api_key:
        print("OPENROUTER_CHAT_API_KEY (or OPENROUTER_API_KEY) is required "
              "(or use --dry-run).", file=sys.stderr)
        return 2
    model = os.environ.get("OPENROUTER_MODEL", MODEL_DEFAULT)
    prompt = build_prompt(report, stats)
    raw = call_llm(prompt, api_key, model)
    try:
        parsed = parse_llm_json(raw)
    except (ValueError, json.JSONDecodeError):
        print("model reply was not valid JSON — retrying once")
        parsed = parse_llm_json(call_llm(prompt, api_key, model))
    parsed, warnings = validate_report(parsed, report)
    out_root = Path(os.environ.get("REPORTER_OUT_DIR", "marketing/outputs"))
    out_dir = write_outputs(out_root, report, parsed, stats, warnings)
    print(f"wrote report ({len(warnings)} validation notes) → {out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
