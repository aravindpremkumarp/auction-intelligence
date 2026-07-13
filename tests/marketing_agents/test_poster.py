"""Unit tests for the Poster content agent's pure logic (no network, no LLM).

Run: pytest tests/marketing_agents -q
"""

import json
import re
from pathlib import Path

import pytest

import marketing_agents.poster as poster
from marketing_agents.poster import (
    MAX_HEADLINE_CHARS,
    MAX_HOOK_CHARS,
    MAX_POST_WORDS,
    MAX_REEL_HOOK_L1_CHARS,
    build_prompt,
    draft_to_island,
    draft_to_reel_island,
    extract_hook,
    parse_llm_json,
    resolve_api_key,
    shape_candidates,
    stats_reel_island,
    step_finalize,
    step_generate,
    validate_drafts,
    write_card_islands,
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
             "pinned_comment": "details: auctionscope.in. not legal advice.",
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

    def test_banned_wording_in_pinned_comment_rejected(self):
        # The honesty rule covers the pinned comment, not just the caption.
        kept, rejected = validate_drafts(
            [self._draft(pinned_comment="link here. guaranteed clean title!")],
            self.CANDS)
        assert not kept and "banned wording" in rejected[0]

    def test_missing_pinned_comment_rejected(self):
        # Required layer: it carries the link + disclaimer. Absent/empty → drop.
        kept, rejected = validate_drafts(
            [self._draft(pinned_comment="  ")], self.CANDS)
        assert not kept and "pinned_comment" in rejected[0]

    def test_clean_pinned_comment_kept(self):
        kept, _ = validate_drafts(
            [self._draft(pinned_comment="details: auctionscope.in. not legal advice.")],
            self.CANDS)
        assert len(kept) == 1

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
             "pinned_comment": "details: auctionscope.in. not legal advice.",
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


class TestHeadlineGates:
    """image_headline is burned onto the card, so it's a published surface:
    honesty-scanned like the caption, and length-capped so it fits."""

    CANDS = shape_candidates([_row("A1")], [], [])

    def _draft(self, **kw):
        d = {"auction_id": "A1", "angle": "closing_soon", "hook_mechanism": "callout",
             "post": "reserve ₹40L, ends 1 Aug.\n\nbody. auctionscope.in",
             "pinned_comment": "details: auctionscope.in. not legal advice.",
             "needs_image": True, "image_headline": "₹40L in Chennai — worth a look?"}
        d.update(kw)
        return d

    def test_banned_word_in_headline_drops_draft(self):
        # Honesty rule covers the card headline, not just the caption.
        kept, rejected = validate_drafts(
            [self._draft(image_headline="Guaranteed title-clear plot, ₹40L")], self.CANDS)
        assert not kept and "banned wording" in rejected[0]

    def test_over_length_headline_with_image_dropped(self):
        kept, rejected = validate_drafts(
            [self._draft(image_headline="₹40L " + "x" * MAX_HEADLINE_CHARS)], self.CANDS)
        assert not kept and "won't fit the card" in rejected[0]

    def test_over_length_headline_without_image_kept(self):
        # No card → the length cap doesn't apply (headline is just a caption note).
        kept, _ = validate_drafts(
            [self._draft(needs_image=False,
                         image_headline="₹40L " + "x" * MAX_HEADLINE_CHARS)], self.CANDS)
        assert len(kept) == 1


class TestDraftToIsland:
    """The poster→card bridge: a validated draft becomes a grounded #data
    island whose headline IS the hook (copy-playbook Part 1, hook surfaces)."""

    def _validated(self, **kw):
        cands = shape_candidates(
            [_row("A1")],
            [_row("D1", reserve=3_800_000, prev=4_500_000, reauction=True)],
            [_row("E1", reserve=900_000)],
        )
        base = {"auction_id": "A1", "angle": "closing_soon", "hook_mechanism": "callout",
                "post": "reserve ₹40L, ends 1 Aug.\n\nbody.", "needs_image": True,
                "pinned_comment": "details: auctionscope.in. not legal advice.",
                "image_headline": "₹40L in Chennai — has it flooded?"}
        base.update(kw)
        kept, _ = validate_drafts([base], cands)
        return kept[0]

    def test_closing_soon_maps_to_deal_card_with_hook_headline(self):
        template, island = draft_to_island(self._validated())
        assert template == "deal-of-the-day-1080"
        assert island["headline"] == "₹40L in Chennai — has it flooded?"
        assert island["reserve_price"] == 4_000_000
        assert island["auction_date"] == "1 Aug 2026"   # ISO → card date
        assert island["market_hint"] == ""              # no comparable → blank, never invented

    def test_price_drop_maps_to_drop_card_with_previous_reserve(self):
        d = self._validated(auction_id="D1", angle="price_drop", hook_mechanism="contrast",
                            image_headline="₹45L → ₹38L. same plot, two months on.")
        template, island = draft_to_island(d)
        assert template == "price-drop-1080x1350"
        assert island["previous_reserve_price"] == 4_500_000
        assert island["reserve_price"] == 3_800_000
        assert island["headline"].startswith("₹45L → ₹38L")

    def test_no_image_returns_none(self):
        assert draft_to_island(self._validated(needs_image=False)) is None

    def test_missing_emd_stays_none_never_invented(self):
        # _row gives emd = reserve/10; force it absent to prove blanking.
        cands = shape_candidates([_row("A1")], [], [])
        cands[0]["emd"] = None
        d = {"auction_id": "A1", "angle": "closing_soon", "hook_mechanism": "callout",
             "post": "reserve ₹40L.\n\nbody.", "needs_image": True,
             "image_headline": "₹40L in Chennai", "source": cands[0]}
        _, island = draft_to_island(d)
        assert island["emd"] is None   # template hides the chip; no stale number

    def test_bad_auction_date_blanks_not_guesses(self):
        cands = shape_candidates([_row("A1", start="garbage")], [], [])
        d = {"auction_id": "A1", "angle": "closing_soon", "hook_mechanism": "callout",
             "post": "reserve ₹40L.\n\nbody.", "needs_image": True,
             "image_headline": "₹40L", "source": cands[0]}
        _, island = draft_to_island(d)
        assert island["auction_date"] == ""

    def test_write_card_islands_emits_files_and_manifest(self, tmp_path):
        drafts = [self._validated(),
                  self._validated(auction_id="D1", angle="price_drop",
                                  hook_mechanism="contrast",
                                  image_headline="₹45L → ₹38L, two months on.")]
        manifest = write_card_islands(tmp_path, drafts)
        assert len(manifest) == 2
        for row in manifest:
            island_path = tmp_path / row["data"]
            assert island_path.exists()
            island = json.loads(island_path.read_text())
            assert island["headline"] == row["headline"]


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
            "pinned_comment": "details: auctionscope.in. not legal advice.",
            "hashtags": ["bankauction"], "needs_image": False,
            "image_headline": "",
            "needs_reel": True,
            "reel_hook": {"line1": "₹40L", "line2": "18 days. one plot."},
            "reel_context_lines": ["a bank set the deadline", "chennai"],
            "engagement_question": "would you check the flood map first?",
            "save_line": "bids close 1 aug — save this"}], "editor_notes": "ok"})

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

    def test_finalize_stages_reel_islands_and_review_block(self, tmp_path, monkeypatch):
        work = self._work_dir(tmp_path, self._response())
        monkeypatch.setenv("POSTER_OUT_DIR", str(tmp_path / "out"))
        assert step_finalize(work) == 0
        (out_dir,) = (tmp_path / "out").iterdir()
        staged = json.loads((out_dir / "drafts.json").read_text())
        # deal reel for the draft + the LLM-free stats reel
        by_id = {r["auction_id"]: r for r in staged["reels"]}
        assert by_id["A1"]["template"] == "deal-reel-1080x1920"
        assert by_id["stats"]["template"] == "stats-reel-1080x1920"
        island = json.loads((out_dir / by_id["A1"]["data"]).read_text())
        assert island["hook"]["line1"] == "₹40L"
        assert island["facts"]["city"] == "Chennai"
        review = (out_dir / "review.md").read_text()
        assert "reel(s) staged" in review and "render_reel.py" in review
        assert "add trending audio in-app" in review

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

    def test_prompt_carries_reel_contract(self):
        cands = shape_candidates([_row("A1")], [], [])
        p = build_prompt({"total_auctions": 1, "upcoming_auctions": 1,
                          "generated_at": "now"}, cands, 5)
        assert "reel_hook" in p and "engagement_question" in p
        assert "Score your 3 candidate hooks" in p  # auto-selection instruction
        assert "needs_reel" in p and "save_line" in p

    def test_prompt_injects_recent_performance_block(self):
        cands = shape_candidates([_row("A1")], [], [])
        p = build_prompt({"total_auctions": 1, "upcoming_auctions": 1,
                          "generated_at": "now"}, cands, 5,
                         recent_performance="by_angle: price_drop: 9.1")
        assert "RECENT PERFORMANCE" in p and "price_drop: 9.1" in p
        # absent by default
        p2 = build_prompt({"total_auctions": 1, "upcoming_auctions": 1,
                           "generated_at": "now"}, cands, 5)
        assert "RECENT PERFORMANCE" not in p2


class TestReelGates:
    """Objective reel gates (copy-playbook.md Part 6). The reel's first frame
    is the whole game, so the budgets are hard drops, not trims."""

    CANDS = shape_candidates([_row("A1")], [], [])

    def _draft(self, **kw):
        d = {"auction_id": "A1", "angle": "closing_soon", "hook_mechanism": "callout",
             "post": "reserve ₹40L, ends 1 Aug.\n\nbody. auctionscope.in",
             "pinned_comment": "details: auctionscope.in. not legal advice.",
             "hashtags": [], "needs_image": False, "image_headline": "",
             "needs_reel": True,
             "reel_hook": {"line1": "₹40L", "line2": "nobody bid."},
             "reel_context_lines": ["didn't sell last round", "the bank cut the reserve"],
             "engagement_question": "would you bid on this?",
             "save_line": "bids close 1 aug — save this"}
        d.update(kw)
        return d

    def test_complete_reel_draft_kept(self):
        kept, rejected = validate_drafts([self._draft()], self.CANDS)
        assert len(kept) == 1 and not rejected

    def test_gates_skipped_when_needs_reel_false(self):
        kept, _ = validate_drafts(
            [self._draft(needs_reel=False, reel_hook={}, engagement_question="")],
            self.CANDS)
        assert len(kept) == 1

    def test_missing_reel_hook_rejected(self):
        kept, rejected = validate_drafts(
            [self._draft(reel_hook={"line1": "", "line2": "x"})], self.CANDS)
        assert not kept and "reel_hook is incomplete" in rejected[0]

    def test_line1_without_figure_rejected(self):
        kept, rejected = validate_drafts(
            [self._draft(reel_hook={"line1": "a bank plot", "line2": "nobody bid."})],
            self.CANDS)
        assert not kept and "no figure" in rejected[0]

    def test_overlong_line1_rejected(self):
        kept, rejected = validate_drafts(
            [self._draft(reel_hook={"line1": "₹" + "9" * MAX_REEL_HOOK_L1_CHARS,
                                    "line2": "nobody bid."})], self.CANDS)
        assert not kept and "won't fit the first frame" in rejected[0]

    def test_throat_clearing_line2_rejected(self):
        kept, rejected = validate_drafts(
            [self._draft(reel_hook={"line1": "₹40L", "line2": "don't miss this one"})],
            self.CANDS)
        assert not kept and "throat-clearing" in rejected[0]

    def test_context_lines_must_be_two(self):
        kept, rejected = validate_drafts(
            [self._draft(reel_context_lines=["only one line"])], self.CANDS)
        assert not kept and "exactly 2" in rejected[0]

    def test_question_must_end_with_question_mark(self):
        kept, rejected = validate_drafts(
            [self._draft(engagement_question="save this for later")], self.CANDS)
        assert not kept and "engagement_question" in rejected[0]

    def test_banned_word_on_reel_surface_rejected(self):
        kept, rejected = validate_drafts(
            [self._draft(save_line="guaranteed deal — save it")], self.CANDS)
        assert not kept and "banned wording" in rejected[0]


class TestDraftToReelIsland:
    """The poster→reel bridge: a validated draft becomes the deal-reel #data
    island, every figure from the grounded source."""

    def _validated(self, **kw):
        cands = shape_candidates(
            [], [_row("D1", reserve=3_800_000, prev=4_500_000, reauction=True)], [])
        d = {"auction_id": "D1", "angle": "price_drop", "hook_mechanism": "contrast",
             "post": "₹45L → ₹38L.\n\nbody. auctionscope.in",
             "pinned_comment": "details: auctionscope.in. not legal advice.",
             "hashtags": [], "needs_image": False, "image_headline": "",
             "needs_reel": True,
             "reel_hook": {"line1": "₹45L → ₹38L", "line2": "nobody bid."},
             "reel_context_lines": ["didn't sell last round", "the bank cut the reserve"],
             "engagement_question": "would you bid on this?",
             "save_line": ""}
        d.update(kw)
        kept, rejected = validate_drafts([d], cands)
        assert kept, rejected
        return kept[0]

    def test_price_drop_island_grounded_from_source(self):
        template, island = draft_to_reel_island(self._validated())
        assert template == "deal-reel-1080x1920"
        assert island["money"]["previous_reserve_price"] == 4_500_000
        assert island["money"]["reserve_price"] == 3_800_000
        assert island["money"]["drop_pct"] == pytest.approx(15.6, abs=0.1)
        assert island["hook"]["line1"] == "₹45L → ₹38L"
        assert island["endcard"]["question"] == "would you bid on this?"
        assert "auction notices" in island["honesty_line"]

    def test_no_reel_when_opted_out(self):
        assert draft_to_reel_island(self._validated(needs_reel=False)) is None

    def test_days_left_none_on_garbage_dates(self):
        assert poster._days_left("garbage") is None
        assert poster._days_left(None) is None
        assert poster._days_left("2026-13-40T00:00:00Z") is None

    def test_island_schema_matches_template_sample(self):
        """Schema-drift guard: the bridge's island keys must equal the keys of
        the sample island inside the committed template (our substitute for
        --strict-variables type validation)."""
        tpl = Path("marketing/templates/deal-reel-1080x1920.html").read_text(
            encoding="utf-8")
        m = re.search(
            r'<script id="data" type="application/json">\s*(\{.*?\})\s*</script>',
            tpl, re.S)
        sample = json.loads(m.group(1))
        _, island = draft_to_reel_island(self._validated())

        def key_tree(d, prefix=""):
            keys = set()
            for k, v in d.items():
                keys.add(prefix + k)
                if isinstance(v, dict):
                    keys |= key_tree(v, prefix + k + ".")
            return keys

        assert key_tree(island) == key_tree(sample)


class TestStatsReelIsland:
    STATS = {"total_auctions": 2179, "upcoming_auctions": 616, "generated_at": "x"}

    def _drafts(self):
        cands = shape_candidates([_row("A1")], [], [])
        return [{"auction_id": "A1", "source": cands[0]}]

    def test_builds_from_stats_and_top_pick(self):
        island = stats_reel_island(self.STATS, self._drafts())
        assert island["stats"][0]["value"] == 616
        assert island["pick"]["reserve_price"] == 4_000_000
        assert "Canara" in island["pick"]["loc"]

    def test_none_when_stats_missing(self):
        assert stats_reel_island({}, self._drafts()) is None
        assert stats_reel_island(self.STATS, []) is None

    def test_schema_matches_stats_template_sample(self):
        tpl = Path("marketing/templates/stats-reel-1080x1920.html").read_text(
            encoding="utf-8")
        m = re.search(
            r'<script id="data" type="application/json">\s*(\{.*?\})\s*</script>',
            tpl, re.S)
        sample = json.loads(m.group(1))
        island = stats_reel_island(self.STATS, self._drafts())
        assert set(island.keys()) == set(sample.keys())
        assert set(island["pick"].keys()) == set(sample["pick"].keys())
        assert set(island["stats"][0].keys()) == set(sample["stats"][0].keys())
