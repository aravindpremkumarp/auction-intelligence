"""Unit tests for the Reporter content agent's pure logic (no network, no LLM).

Run: pytest tests/marketing_agents -q
"""

import pytest

from marketing_agents.reporter import (
    MIN_POSTS,
    build_prompt,
    compute_report,
    engagement_rate,
    load_metrics,
    parse_llm_json,
    validate_report,
)


def _post(pid, *, impressions=1000.0, likes=50.0, comments=10.0, reposts=5.0,
          saves=5.0, link_clicks=0.0, profile_visits=0.0, date="2026-07-01",
          angle="cheapest", fmt="static", platform="instagram"):
    return {
        "post_id": pid, "date": date, "platform": platform, "angle": angle,
        "format": fmt, "impressions": impressions, "likes": likes,
        "comments": comments, "reposts": reposts, "saves": saves,
        "link_clicks": link_clicks, "profile_visits": profile_visits,
    }


# ----------------------------------------------------------------- metrics

def test_engagement_rate_formula():
    p = _post("a", impressions=500, likes=30, comments=10, reposts=5, saves=5)
    # (30+10+5+5)/500*100 = 10.0
    assert engagement_rate(p) == 10.0


def test_engagement_rate_zero_impressions_is_zero():
    assert engagement_rate(_post("a", impressions=0)) == 0.0


def test_load_metrics_parses_and_defaults(tmp_path):
    csv = tmp_path / "m.csv"
    csv.write_text(
        "post_id,date,platform,angle,format,impressions,likes,comments\n"
        "p1,2026-07-01,x,price_drop,reel,1000,40,10\n"
        "p2,2026-07-02,,,,,\n",  # missing numerics -> 0, missing id -> generated later
        encoding="utf-8",
    )
    rows = load_metrics(csv)
    assert rows[0]["post_id"] == "p1"
    assert rows[0]["impressions"] == 1000.0
    assert rows[0]["angle"] == "price_drop"
    # blank numeric cells default to 0, not crash
    assert rows[1]["impressions"] == 0.0
    assert rows[1]["saves"] == 0.0


def test_load_metrics_handles_commas_and_junk(tmp_path):
    csv = tmp_path / "m.csv"
    csv.write_text(
        "post_id,impressions,likes,comments,reposts,saves\n"
        "p1,\"1,200\",n/a,10,5,5\n",
        encoding="utf-8",
    )
    rows = load_metrics(csv)
    assert rows[0]["impressions"] == 1200.0  # comma stripped
    assert rows[0]["likes"] == 0.0           # junk -> 0


# --------------------------------------------------------------- computation

def test_compute_report_ranks_and_baselines():
    posts = [
        _post("hi", impressions=500, likes=50, comments=20, reposts=10, saves=20),  # 20% ER
        _post("lo", impressions=1000, likes=5, comments=0, reposts=0, saves=0),      # 0.5% ER
        _post("mid", impressions=1000, likes=40, comments=10, reposts=5, saves=5),   # 6% ER
        _post("mid2", impressions=800, likes=30, comments=8, reposts=4, saves=4),
        _post("mid3", impressions=1200, likes=48, comments=12, reposts=6, saves=6),
    ]
    r = compute_report(posts)
    assert r["posts_analyzed"] == 5
    assert r["top_performers"][0]["post_id"] == "hi"
    assert r["bottom_performers"][0]["post_id"] == "lo"
    assert r["baseline"]["avg_er"] > 0
    assert set(r["valid_post_ids"]) == {"hi", "lo", "mid", "mid2", "mid3"}


def test_compute_report_breakdown_by_angle():
    posts = [
        _post("a", angle="evaluate", impressions=1000, likes=100, comments=50, reposts=30, saves=40),
        _post("b", angle="evaluate", impressions=1000, likes=90, comments=45, reposts=25, saves=35),
        _post("c", angle="closing_soon", impressions=1000, likes=5, comments=1, reposts=0, saves=1),
    ]
    r = compute_report(posts)
    by_angle = {g["label"]: g for g in r["by_angle"]}
    assert by_angle["evaluate"]["posts"] == 2
    # evaluate should rank above closing_soon (sorted desc by ER)
    assert r["by_angle"][0]["label"] == "evaluate"
    assert by_angle["evaluate"]["avg_er"] > by_angle["closing_soon"]["avg_er"]


def test_trend_detects_direction():
    # first half low ER, second half high ER -> "up"
    posts = [
        _post("d1", date="2026-07-01", impressions=1000, likes=5, comments=0, reposts=0, saves=0),
        _post("d2", date="2026-07-02", impressions=1000, likes=5, comments=0, reposts=0, saves=0),
        _post("d3", date="2026-07-03", impressions=1000, likes=100, comments=50, reposts=30, saves=20),
        _post("d4", date="2026-07-04", impressions=1000, likes=100, comments=50, reposts=30, saves=20),
    ]
    r = compute_report(posts)
    assert r["trend"]["er_direction"] == "up"
    assert r["trend"]["er_delta"] > 0


# --------------------------------------------------------------- validation

def _report_stub(ids):
    return {"valid_post_ids": list(ids), "top_performers": [], "bottom_performers": [],
            "by_angle": [], "by_format": [], "by_platform": [], "baseline": {},
            "trend": None, "posts_analyzed": len(ids)}


def test_validate_drops_unknown_post_ids():
    report = _report_stub(["real1", "real2"])
    parsed = {
        "headline": "ok", "flag": "green", "flag_reason": "x",
        "what_worked": [{"post_id": "real1", "why": "good hook"},
                        {"post_id": "ghost", "why": "should be dropped"}],
        "what_didnt": [], "pattern": "p", "next_week": ["do a thing"], "caveats": "",
    }
    cleaned, warnings = validate_report(parsed, report)
    ids = [w["post_id"] for w in cleaned["what_worked"]]
    assert ids == ["real1"]
    assert any("ghost" in w for w in warnings)


def test_validate_flags_hype_wording():
    report = _report_stub(["p1"])
    parsed = {
        "headline": "guaranteed growth incoming", "flag": "green", "flag_reason": "x",
        "what_worked": [], "what_didnt": [], "pattern": "this will go viral",
        "next_week": ["post"], "caveats": "",
    }
    _, warnings = validate_report(parsed, report)
    assert any("headline" in w for w in warnings)
    assert any("pattern" in w for w in warnings)


def test_validate_requires_actions_and_valid_flag():
    report = _report_stub(["p1"])
    parsed = {"headline": "h", "flag": "purple", "flag_reason": "x",
              "what_worked": [], "what_didnt": [], "pattern": "p",
              "next_week": [], "caveats": ""}
    cleaned, warnings = validate_report(parsed, report)
    assert cleaned["flag"] == "yellow"  # invalid flag defaulted
    assert any("next_week" in w for w in warnings)


# ------------------------------------------------------------------ prompt

def test_build_prompt_contains_grounding_rules():
    r = compute_report([_post(f"p{i}") for i in range(5)])
    prompt = build_prompt(r, {"upcoming_auctions": 600, "total_auctions": 2179})
    assert "NEVER invent" in prompt
    assert "OUR OWN baseline" in prompt
    assert "600 live auctions" in prompt


def test_build_prompt_survives_stats_error():
    r = compute_report([_post(f"p{i}") for i in range(5)])
    prompt = build_prompt(r, {"_stats_error": "boom"})
    assert "unavailable" in prompt


# --------------------------------------------------------------- json parse

def test_parse_llm_json_strips_code_fences():
    raw = '```json\n{"flag": "green", "next_week": ["x"]}\n```'
    assert parse_llm_json(raw)["flag"] == "green"


def test_parse_llm_json_raises_without_object():
    with pytest.raises(ValueError):
        parse_llm_json("no json here")


def test_min_posts_floor_is_five():
    assert MIN_POSTS == 5
