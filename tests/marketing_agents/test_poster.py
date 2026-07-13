"""Unit tests for the Poster content agent's pure logic (no network, no LLM).

Run: pytest tests/marketing_agents -q
"""

import json

import pytest

from marketing_agents.poster import (
    MAX_HOOK_CHARS,
    MAX_POST_WORDS,
    build_prompt,
    extract_hook,
    parse_llm_json,
    resolve_api_key,
    shape_candidates,
    step_finalize,
    step_generate,
    validate_drafts,
)


def _row(aid, *, reserve=4_000_000.0, prev=None, reauction=False, city="Chennai",
         start="2026-08-01T10:00:00Z"):
    return {
        "auction_id": aid,
        "title": f"Property {aid}",
        "url": f"https://example.com/{aid}",
        "reserve_price": reserve,
        "emd": reserve / 10 if reserve else None,
        "auction_start": start,
        "city": city,
        "area": "Somewhere",
        "bank": "Canara Bank",
        "bank_short": "Canara",
        "asset_category": "Residential",
        "property_types": ["Plot"],
        "previous_reserve_price": prev,
        "reauction_count": 1 if reauction else 0,
        "is_reauction": reauction,
    }


class TestShapeCandidates:
    def test_dedupes_and_prefers_price_drop_angle(self):
        drop = _row("A1", reserve=3_800_000, prev=4_500_000, reauction=True)
        same_in_closing = _row("A1")
        out = shape_candidates([same_in_closing], [drop], [])
        assert len(out) == 1
        assert out[0]["angle"] == "price_drop"
        assert out[0]["drop_pct"] == pytest.approx(15.6, abs=0.1)
        assert out[0]["previous_reserve_lakhs"] == 45.0

    def test_price_drops_rank_first_then_closing_then_cheapest(self):
        out = shape_candidates(
            [_row("C1")], [_row("D1", reserve=3_000_000, prev=4_000_000, reauction=True)],
            [_row("E1", reserve=500_000)],
        )
        assert [c["angle"] for c in out] == ["price_drop", "closing_soon", "cheapest"]

    def test_skips_rows_without_price_or_id(self):
        out = shape_candidates([_row("X1", reserve=None), {"title": "no id"}], [], [])
        assert out == []

    def test_reserve_converted_to_lakhs(self):
        out = shape_candidates([_row("A1", reserve=4_014_700)], [], [])
        assert out[0]["reserve_lakhs"] == 40.1


class TestValidateDrafts:
    CANDS = shape_candidates([_row("A1")], [], [])

    def _draft(self, **kw):
        d = {"auction_id": "A1", "angle": "closing_soon", "post": "reserve ₹40L, ends soon. auctionscope.in",
             "hashtags": ["bankauction"], "needs_image": False, "image_headline": ""}
        d.update(kw)
        return d

    def test_valid_draft_kept_with_source_attached(self):
        kept, rejected = validate_drafts([self._draft()], self.CANDS)
        assert len(kept) == 1 and not rejected
        assert kept[0]["source"]["auction_id"] == "A1"

    def test_unknown_auction_id_rejected(self):
        kept, rejected = validate_drafts([self._draft(auction_id="ZZZ")], self.CANDS)
        assert not kept and "unknown auction_id" in rejected[0]

    @pytest.mark.parametrize("bad", [
        "Full due diligence done on this plot.",
        "Our advocate verified it.",
        "Guaranteed clean title!",
        "Title-clear property.",
    ])
    def test_honesty_rule_bans_over_promise(self, bad):
        kept, rejected = validate_drafts([self._draft(post=bad)], self.CANDS)
        assert not kept and "banned wording" in rejected[0]

    def test_over_length_post_rejected(self):
        # Include a figure so it passes "Prove It" and reaches the length gate.
        kept, rejected = validate_drafts(
            [self._draft(post="₹40L " + "word " * (MAX_POST_WORDS + 1))], self.CANDS)
        assert not kept and "words" in rejected[0]

    def test_post_without_a_figure_rejected(self):
        # "Prove It": no price/date/digit is weak copy — dropped.
        kept, rejected = validate_drafts(
            [self._draft(post="a plot in Chennai worth a look. auctionscope.in")],
            self.CANDS)
        assert not kept and "prove it" in rejected[0]

    def test_post_with_a_figure_kept(self):
        kept, _ = validate_drafts(
            [self._draft(post="reserve ₹40L, ends 1 Aug. auctionscope.in")],
            self.CANDS)
        assert len(kept) == 1


class TestHookGates:
    """The stop test's objective slices (copy-playbook.md Part 1): hook length,
    throat-clearing openers, and per-batch mechanism variety."""

    CANDS = shape_candidates([_row("A1"), _row("A2"), _row("A3")], [], [])

    def _draft(self, aid="A1", **kw):
        d = {"auction_id": aid, "angle": "closing_soon",
             "post": "reserve ₹40L, ends 1 Aug.\n\nthe body. auctionscope.in",
             "hashtags": [], "needs_image": False, "image_headline": ""}
        d.update(kw)
        return d

    def test_extract_hook_takes_first_line(self):
        assert extract_hook("₹40L reserve.\n\nlong body here") == "₹40L reserve."

    def test_extract_hook_falls_back_to_first_sentence(self):
        post = "₹40L reserve in Chennai. " + "the notice says a lot more " * 8
        assert extract_hook(post) == "₹40L reserve in Chennai."

    def test_extract_hook_never_splits_decimals(self):
        post = "reserve ₹40.1L, ends 1 Aug. " + "and quite a bit of body text " * 6
        assert extract_hook(post) == "reserve ₹40.1L, ends 1 Aug."

    def test_hook_over_budget_rejected(self):
        # One unbroken 120+ char first line with no sentence break: dies at the fold.
        kept, rejected = validate_drafts(
            [self._draft(post="₹40L " + "x" * (MAX_HOOK_CHARS + 30))], self.CANDS)
        assert not kept and "fold" in rejected[0]

    def test_short_first_sentence_in_single_paragraph_kept(self):
        kept, _ = validate_drafts(
            [self._draft(post="₹40L reserve in Chennai. " + "more context here " * 10)],
            self.CANDS)
        assert len(kept) == 1

    @pytest.mark.parametrize("opener", [
        "Did you know ₹40L buys a plot here? details inside.",
        "🚨 Don't miss this ₹40L reserve, ends 1 Aug.",
        "Imagine owning this ₹40L plot in Chennai.",
    ])
    def test_throat_clearing_openers_rejected(self, opener):
        kept, rejected = validate_drafts([self._draft(post=opener)], self.CANDS)
        assert not kept and "throat-clearing" in rejected[0]

    def test_mechanism_variety_rule_caps_at_two(self):
        drafts = [self._draft(aid=a, hook_mechanism="contrast") for a in ("A1", "A2", "A3")]
        kept, rejected = validate_drafts(drafts, self.CANDS)
        assert [d["auction_id"] for d in kept] == ["A1", "A2"]
        assert len(rejected) == 1 and "variety rule" in rejected[0]

    def test_missing_mechanism_normalized_not_dropped(self):
        kept, _ = validate_drafts([self._draft()], self.CANDS)
        assert kept[0]["hook_mechanism"] == "unspecified"


class TestParseLlmJson:
    def test_plain_json(self):
        assert parse_llm_json('{"drafts": []}') == {"drafts": []}

    def test_fenced_json(self):
        assert parse_llm_json('```json\n{"drafts": []}\n```') == {"drafts": []}

    def test_json_with_leading_prose(self):
        assert parse_llm_json('Here you go:\n{"a": 1}') == {"a": 1}

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            parse_llm_json("no json here at all")


class TestResolveApiKey:
    def test_prefers_chat_key(self):
        env = {"OPENROUTER_CHAT_API_KEY": "chat", "OPENROUTER_API_KEY": "legacy"}
        assert resolve_api_key(env) == "chat"

    def test_falls_back_to_legacy_key(self):
        assert resolve_api_key({"OPENROUTER_API_KEY": "legacy"}) == "legacy"

    def test_empty_chat_key_falls_back(self):
        env = {"OPENROUTER_CHAT_API_KEY": "", "OPENROUTER_API_KEY": "legacy"}
        assert resolve_api_key(env) == "legacy"

    def test_none_when_no_keys(self):
        assert resolve_api_key({}) is None


class TestStagedPipeline:
    """--prepare writes candidates.json + prompt.txt; an engine (Claude Code
    on Max by default, OpenRouter via --generate) writes response.txt;
    --finalize validates and stages. These cover the file round-trip."""

    # last_enriched is null in production /stats — exercise the generated_at fallback.
    STATS = {"total_auctions": 2179, "upcoming_auctions": 616,
             "generated_at": "now", "last_enriched": None}

    def _work_dir(self, tmp_path, response_text):
        work = tmp_path / "work"
        work.mkdir()
        cands = shape_candidates([_row("A1")], [], [])
        (work / "candidates.json").write_text(json.dumps(
            {"stats": self.STATS, "candidates": cands, "max_drafts": 5}),
            encoding="utf-8")
        (work / "response.txt").write_text(response_text, encoding="utf-8")
        return work

    def _response(self):
        return json.dumps({"drafts": [{
            "auction_id": "A1", "angle": "closing_soon",
            "hook_mechanism": "countdown",
            "hook_alternatives": ["₹40L reserve. would you check the flood map first?",
                                  "a bank in Chennai wants ₹40L for this. here's why."],
            "post": "reserve ₹40L, ends soon. auctionscope.in",
            "hashtags": ["bankauction"], "needs_image": False,
            "image_headline": ""}], "editor_notes": "ok"})

    def test_finalize_stages_valid_response(self, tmp_path, monkeypatch):
        work = self._work_dir(tmp_path, self._response())
        monkeypatch.setenv("POSTER_OUT_DIR", str(tmp_path / "out"))
        assert step_finalize(work) == 0
        (out_dir,) = (tmp_path / "out").iterdir()
        staged = json.loads((out_dir / "drafts.json").read_text())
        assert staged["drafts"][0]["auction_id"] == "A1"
        review = (out_dir / "review.md").read_text()
        # last_enriched can be null from the API; fall back to generated_at.
        assert "Data as of now" in review and "None" not in review
        # The hook mechanism + runner-up hooks surface for the human editor.
        assert "hook: `countdown` · alternatives:" in review
        assert "- ₹40L reserve. would you check the flood map first?" in review

    def test_finalize_tolerates_fenced_engine_output(self, tmp_path, monkeypatch):
        work = self._work_dir(tmp_path, f"```json\n{self._response()}\n```")
        monkeypatch.setenv("POSTER_OUT_DIR", str(tmp_path / "out"))
        assert step_finalize(work) == 0

    def test_finalize_fails_when_all_drafts_rejected(self, tmp_path, monkeypatch):
        bad = json.dumps({"drafts": [{"auction_id": "A1", "angle": "closing_soon",
                                      "post": "Guaranteed title-clear deal!"}]})
        work = self._work_dir(tmp_path, bad)
        monkeypatch.setenv("POSTER_OUT_DIR", str(tmp_path / "out"))
        assert step_finalize(work) == 1
        (out_dir,) = (tmp_path / "out").iterdir()
        staged = json.loads((out_dir / "drafts.json").read_text())
        assert staged["drafts"] == [] and staged["rejected"]

    def test_finalize_honors_explicit_response_path(self, tmp_path, monkeypatch):
        work = self._work_dir(tmp_path, "not used")
        alt = tmp_path / "claude-reply.txt"
        alt.write_text(self._response(), encoding="utf-8")
        monkeypatch.setenv("POSTER_OUT_DIR", str(tmp_path / "out"))
        assert step_finalize(work, alt) == 0

    def test_generate_without_any_key_fails_cleanly(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENROUTER_CHAT_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        assert step_generate(tmp_path) == 2


class TestPrompt:
    def test_prompt_carries_data_and_rules(self):
        cands = shape_candidates([_row("A1")], [], [])
        p = build_prompt({"total_auctions": 2179, "upcoming_auctions": 616,
                          "generated_at": "now", "last_enriched": "today"}, cands, 5)
        assert "A1" in p and "616" in p
        assert "due diligence" in p  # listed as banned
        assert "HOOK SYSTEM" in p and "QUALITY BAR" in p  # playbook injected
        assert "THE STOP TEST" in p  # hooks are engineered against the 4 gates
        assert "hook_alternatives" in p and "hook_mechanism" in p  # 3-variant contract
        assert "countdown" in p and "mistake" in p  # mechanism menu present
        assert "Prove It" in p  # the number-does-the-work gate is taught
        assert json.loads(json.dumps(cands))  # candidates serialize cleanly
