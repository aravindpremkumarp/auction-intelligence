"""Unit tests for the Poster content agent's pure logic (no network, no LLM).

Run: pytest tests/marketing_agents -q
"""

import json

import pytest

from marketing_agents.poster import (
    MAX_POST_WORDS,
    build_prompt,
    parse_llm_json,
    shape_candidates,
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
        kept, rejected = validate_drafts(
            [self._draft(post="word " * (MAX_POST_WORDS + 1))], self.CANDS)
        assert not kept and "words" in rejected[0]


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


class TestPrompt:
    def test_prompt_carries_data_and_rules(self):
        cands = shape_candidates([_row("A1")], [], [])
        p = build_prompt({"total_auctions": 2179, "upcoming_auctions": 616,
                          "generated_at": "now", "last_enriched": "today"}, cands, 5)
        assert "A1" in p and "616" in p
        assert "due diligence" in p  # listed as banned
        assert json.loads(json.dumps(cands))  # candidates serialize cleanly
