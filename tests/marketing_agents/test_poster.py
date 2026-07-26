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


class TestPriceDropFloor:
    """A drop only counts as a price_drop angle when it is big enough to see."""

    def test_trivial_drop_is_not_a_price_drop(self):
        # The real 798444 case: ₹31.9L -> ₹31L, a 2.7% / ₹87k move that used to
        # be handed to the model as its best contrast candidate.
        assert not poster._is_price_drop(
            _row("A1", reserve=3_100_000, prev=3_187_000, reauction=True))

    def test_percentage_floor_admits_a_real_cut(self):
        assert poster._is_price_drop(
            _row("A1", reserve=3_800_000, prev=4_500_000, reauction=True))

    def test_absolute_floor_admits_a_small_percentage_on_a_big_lot(self):
        # ₹2.5L off ₹1Cr is 2.5% — under the percentage floor, still a real cut.
        assert poster._is_price_drop(
            _row("A1", reserve=9_750_000, prev=10_000_000, reauction=True))

    def test_not_a_reauction_is_never_a_drop(self):
        assert not poster._is_price_drop(
            _row("A1", reserve=3_000_000, prev=4_000_000, reauction=False))

    def test_price_rise_is_never_a_drop(self):
        assert not poster._is_price_drop(
            _row("A1", reserve=4_000_000, prev=3_000_000, reauction=True))

    def test_trivial_drop_still_ships_under_an_honest_angle(self):
        """Below the floor the lot is not dropped — it just stops claiming a
        discount. fetch_pool would route it here via closing_soon."""
        row = _row("A1", reserve=3_100_000, prev=3_187_000, reauction=True)
        out = shape_candidates([row], [], [])
        assert len(out) == 1
        assert out[0]["angle"] == "closing_soon"
        # and the card gets no strike-through it would have to justify
        assert "previous_reserve_price" not in out[0]


class TestCityContext:
    """Where ONE lot sits among the live lots of its own city. The city figure
    is the yardstick for a property post; a post whose SUBJECT is the city is
    the carousel's job."""

    def _market(self):
        # Chennai: 1L, 2L, 3L, 4L, 5L, 6L (median 3.5L). Salem: 3 rows, which
        # is under the floor. Areas: A2/A4 share "Anna Nagar".
        rows = [_row(f"C{i}", reserve=i * 100_000.0) for i in range(1, 7)]
        rows[1]["area"] = rows[3]["area"] = "Anna Nagar"
        rows += [_row(f"S{i}", reserve=i * 100_000.0, city="Salem") for i in range(1, 4)]
        return rows

    def test_ranks_the_lot_inside_its_own_city(self):
        market = self._market()
        ctx = poster.city_context(market[1], market)  # ₹2L, 2nd of 6
        assert ctx["city_total"] == 6
        assert (ctx["rank"], ctx["cheaper"], ctx["dearer"]) == (2, 1, 4)

    def test_median_and_distance_from_it(self):
        market = self._market()
        ctx = poster.city_context(market[1], market)
        assert ctx["median_lakhs"] == pytest.approx(3.5)
        assert ctx["vs_median_lakhs"] == pytest.approx(-1.5)

    def test_cheaper_than_pct_is_the_share_strictly_dearer(self):
        market = self._market()
        ctx = poster.city_context(market[1], market)
        assert ctx["cheaper_than_pct"] == round(100 * 4 / 6)

    def test_area_count_is_within_the_city(self):
        market = self._market()
        assert poster.city_context(market[1], market)["area_count"] == 2
        assert poster.city_context(market[0], market)["area_count"] == 4

    def test_other_cities_are_not_counted(self):
        market = self._market()
        assert poster.city_context(market[0], market)["city_total"] == 6

    def test_thin_city_gets_no_context(self):
        """A rank out of three lots is arithmetic, not a story."""
        market = self._market()
        salem = next(r for r in market if r["city"] == "Salem")
        assert poster.city_context(salem, market) is None

    def test_row_outside_the_market_gets_no_context(self):
        """Otherwise `dearer` would silently be off by one."""
        market = self._market()
        stranger = _row("Z9", reserve=250_000.0)
        assert poster.city_context(stranger, market) is None

    def test_no_market_no_context(self):
        assert poster.city_context(_row("C1"), []) is None

    def test_shape_candidates_attaches_it_only_when_given_a_market(self):
        market = self._market()
        with_ctx = shape_candidates([market[1]], [], [], market=market)
        assert with_ctx[0]["city_context"]["rank"] == 2
        without = shape_candidates([market[1]], [], [])
        assert "city_context" not in without[0]


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

    def test_card_omits_bank_and_emd_and_uses_locality(self):
        # Bank + EMD are intentionally dropped from the card: EMD is ~always 10%
        # of reserve (derivable, adds nothing) and the bank isn't a scroll-
        # stopper. The sub-line is the locality (area), not the raw bank-led
        # auction title.
        cands = shape_candidates([_row("A1")], [], [])   # area="Somewhere", city="Chennai"
        d = {"auction_id": "A1", "angle": "closing_soon", "hook_mechanism": "callout",
             "post": "reserve ₹40L.\n\nbody.", "needs_image": True,
             "image_headline": "₹40L in Chennai", "source": cands[0]}
        _, island = draft_to_island(d)
        assert "emd" not in island
        assert "bank" not in island
        assert island["title"] == "Somewhere"            # locality, not "Property A1"

    def test_locality_blank_when_same_as_city(self):
        cands = shape_candidates([_row("A1", city="Salem")], [], [])
        cands[0]["area"] = "Salem"                        # area == city → redundant
        d = {"auction_id": "A1", "angle": "closing_soon", "hook_mechanism": "callout",
             "post": "reserve ₹40L.\n\nbody.", "needs_image": True,
             "image_headline": "₹40L", "source": cands[0]}
        _, island = draft_to_island(d)
        assert island["title"] == ""                      # sub-line hidden

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


def _carousel_rows(city="Salem", n=5, types=("Plot",), start="2027-08-01T10:00:00Z"):
    """A price-ascending live page for one city — what fetch_pool hands
    select_carousel. Reserves 15L, 16L, … so every figure is predictable."""
    rows = []
    for i in range(n):
        row = _row(f"{city[:2].upper()}{i}", reserve=1_500_000 + i * 100_000,
                   city=city, start=start)
        row["property_types"] = list(types)
        rows.append(row)
    return rows


class TestSelectCarousel:
    """Slide selection is pure data — the model never picks a property. The
    cover's "cheapest in <city>" claim rests on the page being price-ascending."""

    def test_picks_the_city_with_most_lots_cheapest_first(self):
        rows = _carousel_rows("Salem", 5) + _carousel_rows("Erode", 4)
        facts = poster.select_carousel(rows)
        assert facts["city"] == "Salem"
        assert facts["asset_label"] == "plots"
        assert facts["count"] == 5
        assert [p["reserve_lakhs"] for p in facts["properties"]] == [15.0, 16.0, 17.0, 18.0, 19.0]

    def test_caps_at_max_slides(self):
        facts = poster.select_carousel(_carousel_rows("Salem", 9))
        assert facts["count"] == poster.MAX_CAROUSEL_SLIDES
        # …and keeps the cheapest ones, not the first nine.
        assert facts["properties"][0]["reserve_lakhs"] == 15.0

    def test_none_when_no_city_has_enough_lots(self):
        rows = _carousel_rows("Salem", 3) + _carousel_rows("Erode", 2)
        assert poster.select_carousel(rows) is None

    def test_mixed_types_fall_back_to_a_generic_but_true_label(self):
        # 2 plots + 3 flats in one city: no type reaches the minimum, so the
        # claim widens to "properties" rather than becoming false.
        rows = _carousel_rows("Salem", 2, types=("Plot",))
        rows += _carousel_rows("Salem", 3, types=("Flat",))
        facts = poster.select_carousel(rows)
        assert facts["asset_label"] == poster.MIXED_ASSET_LABEL
        assert facts["count"] == 5

    def test_single_type_group_beats_a_bigger_mixed_one(self):
        rows = _carousel_rows("Salem", 4, types=("Flat",)) + _carousel_rows("Salem", 2, types=("Shop",))
        facts = poster.select_carousel(rows)
        assert facts["asset_label"] == "flats"      # the sharper claim wins
        assert facts["count"] == 4

    def test_skips_rows_without_price_or_city(self):
        rows = _carousel_rows("Salem", 5)
        rows[0]["reserve_price"] = None
        rows[1]["city"] = ""
        assert poster.select_carousel(rows) is None   # only 3 usable left

    def test_locality_blank_when_area_repeats_the_city(self):
        rows = _carousel_rows("Salem", 4)
        for r in rows:
            r["area"] = "salem"
        facts = poster.select_carousel(rows)
        assert all(p["locality"] == "" for p in facts["properties"])

    def test_figures_span_covers_reserves_and_emds(self):
        facts = poster.select_carousel(_carousel_rows("Salem", 4))
        figs = facts["figures_lakhs"]
        assert min(figs) == 1.5 and max(figs) == 18.0   # emd of the cheapest → priciest reserve

    def test_week_label_is_honest_about_timing(self):
        from datetime import date, timedelta
        soon = (date.today() + timedelta(days=3)).isoformat() + "T10:00:00Z"
        assert poster.select_carousel(_carousel_rows("Salem", 4, start=soon))["week_label"] == "this week"
        far = (date.today() + timedelta(days=40)).isoformat() + "T10:00:00Z"
        assert poster.select_carousel(_carousel_rows("Salem", 4, start=far))["week_label"] == "right now"

    def test_selection_is_deterministic(self):
        rows = _carousel_rows("Salem", 4) + _carousel_rows("Erode", 4)
        first = poster.select_carousel(rows)
        assert poster.select_carousel(list(reversed(rows)))["city"] == first["city"]


class TestCarouselIsland:
    def test_island_matches_the_template_contract(self):
        facts = poster.select_carousel(_carousel_rows("Salem", 4))
        island = poster.carousel_island(facts, "4 plots under ₹19L in salem")
        assert set(island) == {"city", "asset_label", "week_label", "headline",
                               "listing_note", "properties"}
        assert island["headline"] == "4 plots under ₹19L in salem"
        assert len(island["properties"]) == 4
        # Slides carry only what the template renders — no auction_id, no emd.
        assert set(island["properties"][0]) == {"title", "locality", "bank",
                                                "reserve_price", "auction_date"}
        assert island["properties"][0]["title"] == "Residential Plot"
        assert "Salem" in island["listing_note"]

    def test_blank_headline_lets_the_cover_keep_its_formula_title(self):
        facts = poster.select_carousel(_carousel_rows("Salem", 4))
        assert poster.carousel_island(facts)["headline"] == ""


class TestValidateCarousel:
    """The slides are grounded by construction; this gates only what the model
    wrote — same honesty/hook rules as a caption, plus carousel grounding."""

    CITIES = {"Salem", "Chennai", "Erode"}

    def _facts(self, **kw):
        facts = poster.select_carousel(_carousel_rows("Salem", 5))
        facts.update(kw)
        return facts

    def _copy(self, **kw):
        base = {
            "headline": "5 plots in salem, from ₹15L",
            "post": ("5 plots in salem start at ₹15L.\n\n"
                     "the cheapest is on slide 2. every reserve here comes "
                     "from the bank's own notice. more on auctionscope.in."),
            "pinned_comment": "full list: auctionscope.in. not legal advice — verify with the bank. which one would you check first?",
            "hashtags": ["bankauction", "salem"], "hook_mechanism": "callout",
        }
        base.update(kw)
        return base

    def _run(self, **kw):
        return poster.validate_carousel(self._copy(**kw), self._facts(), self.CITIES)

    def test_clean_copy_kept(self):
        copy, reasons = self._run()
        assert reasons == [] and copy["hook_mechanism"] == "callout"

    def test_absent_block_is_not_an_error(self):
        assert poster.validate_carousel(None, self._facts()) == (None, [])

    def test_no_facts_means_no_carousel(self):
        assert poster.validate_carousel(self._copy(), None) == (None, [])

    def test_banned_wording_drops_it(self):
        copy, reasons = self._run(post="5 title-clear plots in salem from ₹15L.\n\nbody.")
        assert copy is None and "banned wording" in reasons[0]

    def test_must_name_the_city_it_shows(self):
        copy, reasons = self._run(post="5 plots from ₹15L.\n\nbody.")
        assert copy is None and "never names the city" in reasons[0]

    def test_naming_another_city_drops_it(self):
        copy, reasons = self._run(
            post="5 plots in salem from ₹15L.\n\ncheaper than anything in Chennai.")
        assert copy is None and "names another city (Chennai)" in reasons[0]

    def test_figure_outside_the_slides_drops_it(self):
        copy, reasons = self._run(post="5 plots in salem from ₹95L.\n\nbody.")
        assert copy is None and "outside the slides" in reasons[0]

    def test_rounded_figure_within_slack_is_kept(self):
        # ₹1.5L is the cheapest EMD; the voice rounds, so the bound has slack.
        copy, _ = self._run(post="5 plots in salem, emd from ₹1.5L.\n\nbody.")
        assert copy is not None

    def test_wrong_count_drops_it(self):
        copy, reasons = self._run(post="7 plots in salem from ₹15L.\n\nbody.")
        assert copy is None and "5 are on the slides" in reasons[0]

    def test_over_length_cover_headline_drops_it(self):
        copy, reasons = self._run(headline="x" * (MAX_HEADLINE_CHARS + 1))
        assert copy is None and "won't fit slide 1" in reasons[0]

    def test_missing_pinned_comment_drops_it(self):
        copy, reasons = self._run(pinned_comment="")
        assert copy is None and "missing pinned_comment" in reasons[0]

    def test_throat_clearing_hook_drops_it(self):
        copy, reasons = self._run(post="did you know salem has 5 plots from ₹15L?\n\nbody.")
        assert copy is None and "throat-clearing" in reasons[0]

    def test_post_without_a_figure_drops_it(self):
        copy, reasons = self._run(post="plots in salem, cheap.\n\nbody.")
        assert copy is None and "no concrete figure" in reasons[0]


class TestFiguresInLakhs:
    """The range check is only as good as the parser reading the copy's ₹."""

    @pytest.mark.parametrize("text,expected", [
        ("₹38.5L", [38.5]),
        ("₹1.2 Cr", [120.0]),          # crore → lakhs
        ("₹38,50,000", [38.5]),        # bare rupees, en-IN grouping
        ("₹15 lakh", [15.0]),          # long unit, not a bare "l"
        ("from ₹15L to ₹19L", [15.0, 19.0]),
    ])
    def test_reads_every_form_the_voice_uses(self, text, expected):
        assert poster._figures_in_lakhs(text) == expected

    @pytest.mark.parametrize("text", ["₹15 lots", "₹5", "5 plots, no rupees"])
    def test_skips_what_it_cannot_read_rather_than_guessing(self, text):
        # A guess here would invent an out-of-range figure and drop good copy.
        assert poster._figures_in_lakhs(text) == []


class TestWriteCarouselIsland:
    def test_writes_island_and_manifest_row(self, tmp_path):
        facts = poster.select_carousel(_carousel_rows("Salem", 4))
        row = poster.write_carousel_island(tmp_path, facts, {"headline": "4 plots in salem"})
        assert row["template"] == poster.CAROUSEL_TEMPLATE
        assert row["draft_index"] == 0 and row["auction_id"] == "carousel"
        assert row["slides"] == 6                      # cover + 4 + CTA
        island = json.loads((tmp_path / row["data"]).read_text())
        assert island["headline"] == "4 plots in salem"
        assert len(island["properties"]) == 4

    def test_no_copy_writes_nothing(self, tmp_path):
        facts = poster.select_carousel(_carousel_rows("Salem", 4))
        assert poster.write_carousel_island(tmp_path, facts, None) is None
        assert not (tmp_path / "cards").exists()


class TestCarouselPrompt:
    def test_brief_lists_the_slides_and_the_allowed_figures(self):
        facts = poster.select_carousel(_carousel_rows("Salem", 4))
        block = poster.carousel_prompt_block(facts)
        assert "CAROUSEL" in block and "Salem" in block
        assert "₹15.0L" in block                       # slide figures are spelled out
        assert 'Say "4"' in block                      # the count it must not invent

    def test_no_qualifying_city_omits_the_brief(self):
        assert poster.carousel_prompt_block(None) == ""
        prompt = build_prompt({"upcoming_auctions": 1, "total_auctions": 2},
                              shape_candidates([_row("A1")], [], []), 5)
        assert "CAROUSEL (exactly one per batch" not in prompt
        # The output schema still describes the optional key, and tells the
        # model to omit it when no brief appeared — so a stray carousel object
        # can't sneak in ungrounded.
        assert "if no CAROUSEL brief appears above" in prompt


class TestPropertyCarousel:
    """The --auction-id kit's swipe post. It introduces NO new model output —
    the cover hook is the image_headline that already led the caption and the
    card, so it inherits that field's honesty scan and length cap."""

    def _validated(self, **kw):
        cands = shape_candidates(
            [_row("A1")],
            [_row("D1", reserve=3_800_000, prev=4_500_000, reauction=True)], [])
        base = {"auction_id": "A1", "angle": "closing_soon", "hook_mechanism": "callout",
                "post": "reserve ₹40L, ends 1 Aug.\n\nbody.", "needs_image": True,
                "pinned_comment": "details: auctionscope.in. not legal advice.",
                "image_headline": "₹40L in Chennai — has it flooded?"}
        base.update(kw)
        kept, _ = validate_drafts([base], cands)
        return kept[0]

    def test_reuses_the_caption_hook_as_the_cover(self):
        template, island = poster.draft_to_property_carousel(self._validated())
        assert template == poster.PROPERTY_CAROUSEL_TEMPLATE
        assert island["headline"] == "₹40L in Chennai — has it flooded?"
        assert island["eyebrow"] == "Live auction"
        assert island["asset_type"] == "Residential Plot"
        assert island["reserve_price"] == 4_000_000
        assert island["emd"] == 400_000
        assert island["auction_date"] == "1 Aug 2026"

    def test_price_drop_flags_the_eyebrow_and_keeps_the_earlier_reserve(self):
        d = self._validated(auction_id="D1", angle="price_drop",
                            hook_mechanism="contrast", image_headline="₹45L → ₹38L")
        _, island = poster.draft_to_property_carousel(d)
        assert island["eyebrow"] == "Price drop · re-auction"
        assert island["previous_reserve_price"] == 4_500_000

    def test_earlier_reserve_not_lower_is_cleared(self):
        d = self._validated()
        d["source"] = {**d["source"], "previous_reserve_price": 1_000_000}  # went UP
        _, island = poster.draft_to_property_carousel(d)
        assert island["previous_reserve_price"] is None
        assert island["eyebrow"] == "Live auction"

    def test_locality_blank_when_it_repeats_the_city(self):
        d = self._validated()
        d["source"] = {**d["source"], "area": "chennai", "city": "Chennai"}
        _, island = poster.draft_to_property_carousel(d)
        assert island["locality"] == ""

    def test_no_reserve_means_no_carousel(self):
        d = self._validated()
        d["source"] = {**d["source"], "reserve_price": None}
        assert poster.draft_to_property_carousel(d) is None

    def test_write_emits_island_and_manifest(self, tmp_path):
        rows = poster.write_property_carousel(tmp_path, [self._validated()])
        assert len(rows) == 1 and rows[0]["slides"] == 6
        island = json.loads((tmp_path / rows[0]["data"]).read_text())
        assert island["headline"] == rows[0]["headline"]
        # Filename is distinct from the plain card's so they never collide.
        assert rows[0]["data"].endswith("-carousel.json")


class TestFullKitGating:
    """A normal batch must NOT emit five property carousels — that is 30 extra
    slides through a review gate that publishes about five posts a week."""

    STATS = {"total_auctions": 10, "upcoming_auctions": 5, "generated_at": "now"}

    def _drafts(self):
        cands = shape_candidates([_row("A1")], [], [])
        base = {"auction_id": "A1", "angle": "closing_soon", "hook_mechanism": "callout",
                "post": "reserve ₹40L.\n\nbody.", "needs_image": True,
                "pinned_comment": "auctionscope.in. not legal advice.",
                "image_headline": "₹40L in Chennai"}
        kept, _ = validate_drafts([base], cands)
        return kept

    def test_batch_mode_emits_no_property_carousel(self, tmp_path):
        out = poster.write_outputs(tmp_path, self.STATS, self._drafts(), [], "")
        staged = json.loads((out / "drafts.json").read_text())
        assert all(not c["data"].endswith("-carousel.json") for c in staged["cards"])
        assert "carousel:" not in (out / "review.md").read_text()

    def test_full_kit_emits_it_alongside_the_card(self, tmp_path):
        out = poster.write_outputs(tmp_path, self.STATS, self._drafts(), [], "",
                                   full_kit=True)
        staged = json.loads((out / "drafts.json").read_text())
        templates = [c["template"] for c in staged["cards"]]
        assert poster.PROPERTY_CAROUSEL_TEMPLATE in templates
        assert "deal-of-the-day-1080" in templates      # the card still ships
        review = (out / "review.md").read_text()
        assert "check before you bid" in review
        # Card and carousel share a draft_index but must both be reported.
        assert "card: `deal-of-the-day-1080`" in review


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

    def _work_dir(self, tmp_path, response_text, carousel=None):
        work = tmp_path / "work"
        work.mkdir()
        cands = shape_candidates([_row("A1")], [], [])
        (work / "candidates.json").write_text(json.dumps(
            {"stats": self.STATS, "candidates": cands, "carousel": carousel,
             "max_drafts": 5}),
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

    def _carousel_response(self, **copy):
        block = {"headline": "5 plots in salem, from ₹15L",
                 "post": ("5 plots in salem start at ₹15L.\n\nthe cheapest is on "
                          "slide 2. reserves from each bank's notice. auctionscope.in."),
                 "pinned_comment": "full list: auctionscope.in. not legal advice.",
                 "hashtags": ["bankauction", "salem"], "location_tag": "Salem",
                 "hook_mechanism": "callout"}
        block.update(copy)
        return json.dumps({**json.loads(self._response()), "carousel": block})

    def test_finalize_stages_the_carousel(self, tmp_path, monkeypatch):
        facts = poster.select_carousel(_carousel_rows("Salem", 5))
        work = self._work_dir(tmp_path, self._carousel_response(), carousel=facts)
        monkeypatch.setenv("POSTER_OUT_DIR", str(tmp_path / "out"))
        assert step_finalize(work) == 0
        (out_dir,) = (tmp_path / "out").iterdir()
        staged = json.loads((out_dir / "drafts.json").read_text())
        # It rides in the same `cards` manifest, first — so --render-staged
        # picks it up with no special case.
        row = staged["cards"][0]
        assert row["template"] == poster.CAROUSEL_TEMPLATE and row["draft_index"] == 0
        island = json.loads((out_dir / row["data"]).read_text())
        assert island["city"] == "Salem" and len(island["properties"]) == 5
        review = (out_dir / "review.md").read_text()
        assert "## Carousel — 5 cheapest plots — Salem" in review
        assert "7 slides (cover + 5 + CTA)" in review
        assert "slides, cheapest first" in review

    def test_finalize_drops_ungrounded_carousel_but_keeps_the_batch(self, tmp_path, monkeypatch):
        facts = poster.select_carousel(_carousel_rows("Salem", 5))
        work = self._work_dir(
            tmp_path,
            self._carousel_response(post="5 plots in salem from ₹95L.\n\nbody."),
            carousel=facts)
        monkeypatch.setenv("POSTER_OUT_DIR", str(tmp_path / "out"))
        assert step_finalize(work) == 0            # the captions still ship
        (out_dir,) = (tmp_path / "out").iterdir()
        staged = json.loads((out_dir / "drafts.json").read_text())
        assert staged["carousel"] is None
        assert all(c["auction_id"] != "carousel" for c in staged["cards"])
        assert any("outside the slides" in r for r in staged["rejected"])

    def test_finalize_without_a_carousel_is_unchanged(self, tmp_path, monkeypatch):
        work = self._work_dir(tmp_path, self._response())      # no carousel key
        monkeypatch.setenv("POSTER_OUT_DIR", str(tmp_path / "out"))
        assert step_finalize(work) == 0
        (out_dir,) = (tmp_path / "out").iterdir()
        staged = json.loads((out_dir / "drafts.json").read_text())
        assert staged["carousel"] is None
        assert "## Carousel" not in (out_dir / "review.md").read_text()

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


class TestHookDatabase:
    """marketing/hooks.json — the curated hook arsenal (copy-playbook Part 1).
    Every string here is a candidate published surface, so it passes the same
    objective gates the Poster enforces at draft time."""

    DATA = json.loads(Path("marketing/hooks.json").read_text(encoding="utf-8"))
    PILLARS = ("deals", "risk", "market_data", "education", "news", "geo",
               "evaluation", "qa", "build_in_public")

    def _all_hooks(self):
        for pillar, hooks in self.DATA["pillars"].items():
            for h in hooks:
                yield pillar, h

    def test_all_pillars_present_with_depth(self):
        assert set(self.DATA["pillars"].keys()) == set(self.PILLARS)
        for pillar in self.PILLARS:
            assert len(self.DATA["pillars"][pillar]) >= 8, pillar

    # Budgets are on-screen budgets, so measure a REALISTIC EXPANSION of each
    # {placeholder}, not the template string (placeholder names are long).
    EXPANSIONS = {"city": "Coimbatore", "area": "Ambattur", "type": "flat",
                  "district": "Tiruvallur", "metro_line": "blue",
                  "bank": "Canara", "topic": "repo rate",
                  "headline_short": "RBI holds the repo rate",
                  "question_short": "which are re-auctions?",
                  "misconception": "auctions mean court trouble",
                  "feature": "price-drop alerts",
                  "feature_idea": "saved searches",
                  "bug_effect": "hid 12 listings", "theme": "possession",
                  "check": "possession", "date": "24 Jul",
                  "now": "27.8", "prev": "31.9", "emd": "2.78",
                  "median": "42.4", "cheaper": "11", "dearer": "57",
                  "pct_cheaper": "83", "area_count": "11",
                  "city_total": "69", "emd_pct": "10"}

    def _expand(self, text):
        return re.sub(r"\{(\w+)\}",
                      lambda m: self.EXPANSIONS.get(m.group(1), "123"), text)

    def test_budgets_respected_on_every_surface(self):
        b = self.DATA["budgets"]
        for pillar, h in self._all_hooks():
            ident = f"{pillar}: {h['caption'][:40]}"
            assert len(self._expand(h["caption"])) <= b["caption"], ident
            assert len(self._expand(h["reel"]["line1"])) <= b["reel_line1"], ident
            assert len(self._expand(h["reel"]["line2"])) <= b["reel_line2"], ident
            assert len(self._expand(h["headline"])) <= b["headline"], ident

    def test_reel_line1_carries_a_figure_or_placeholder(self):
        # The number does the work on frame 0 — literal digit, ₹, or a
        # {placeholder} that expands to one.
        for pillar, h in self._all_hooks():
            l1 = h["reel"]["line1"]
            assert re.search(r"[\d₹{]", l1), f"{pillar}: {l1}"

    def test_honesty_rule_on_every_string(self):
        import marketing_agents.poster as p
        banned = [re.compile(pat, re.IGNORECASE) for pat in p.BANNED_PATTERNS]
        for pillar, h in self._all_hooks():
            blob = " ".join([h["caption"], h["reel"]["line1"],
                             h["reel"]["line2"], h["headline"]])
            hits = [pat.pattern for pat in banned if pat.search(blob)]
            assert not hits, f"{pillar}: {hits} in {blob[:60]}"

    def test_no_throat_clearing_openers(self):
        import marketing_agents.poster as p
        for pillar, h in self._all_hooks():
            for text in (h["caption"], h["reel"]["line2"]):
                lead = re.sub(r"^[^0-9a-zA-Z₹]+", "", text).lower()
                assert not lead.startswith(p.BANNED_OPENERS), f"{pillar}: {text}"

    def test_mechanisms_are_valid(self):
        valid = set(self.DATA["mechanisms"])
        assert valid == set(poster.HOOK_MECHANISMS)
        for pillar, h in self._all_hooks():
            assert h["mechanism"] in valid, f"{pillar}: {h['mechanism']}"

    def test_generated_doc_is_in_sync(self):
        import subprocess
        import sys as _sys
        r = subprocess.run(
            [_sys.executable, "marketing/gen_hook_doc.py", "--check"],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_load_hooks_feeds_the_prompt(self):
        arsenal = poster.load_hooks()
        assert "[contrast]" in arsenal and "₹{prev}L → ₹{now}L" in arsenal
        cands = shape_candidates([_row("A1")], [], [])
        p = build_prompt({"total_auctions": 1, "upcoming_auctions": 1,
                          "generated_at": "now"}, cands, 5)
        assert "HOOK ARSENAL" in p and "marketing/hooks.json" in p

    def test_load_hooks_missing_file_degrades(self, monkeypatch):
        monkeypatch.setattr(poster, "HOOKS_PATH", Path("/nonexistent/hooks.json"))
        assert poster.load_hooks() == ""


class TestPillarSelection:
    """The arsenal is selected from the angles in the pool, not hardcoded."""

    def test_base_pillars_always_present(self):
        cands = shape_candidates([_row("A1")], [], [])
        assert poster.pillars_for(cands)[:3] == ("deals", "risk", "market_data")

    def test_risk_pillar_reaches_every_batch(self):
        """What the reserve does not cover is true of every lot, so the
        warning hooks must not depend on an angle being present."""
        for cands in (shape_candidates([_row("A1")], [], []),
                      shape_candidates([], [], [_row("E1", reserve=5e5)])):
            assert "risk" in poster.pillars_for(cands)

    def test_cheapest_pulls_in_geo_and_evaluation(self):
        cands = shape_candidates([], [], [_row("E1", reserve=500_000)])
        assert set(poster.pillars_for(cands)) >= {"geo", "evaluation"}

    def test_price_drop_pulls_in_education(self):
        cands = shape_candidates(
            [], [_row("D1", reserve=3_000_000, prev=4_000_000, reauction=True)], [])
        assert "education" in poster.pillars_for(cands)

    def test_pillars_are_unique_and_stable(self):
        cands = shape_candidates(
            [], [], [_row("E1", reserve=5e5), _row("E2", reserve=6e5)])
        got = poster.pillars_for(cands)
        assert len(got) == len(set(got))
        assert got == poster.pillars_for(cands)

    def test_placeholder_only_pillars_never_reach_the_prompt(self):
        """news / qa / build_in_public interpolate fields a draft has no source
        for ({headline_short}, {question_short}, {total}) — they must stay out."""
        every_angle = shape_candidates(
            [_row("C1")], [_row("D1", reserve=3e6, prev=4e6, reauction=True)],
            [_row("E1", reserve=5e5)])
        assert set(poster.pillars_for(every_angle)).isdisjoint(
            {"news", "qa", "build_in_public"})

    def test_geo_hooks_reach_the_prompt_for_a_cheapest_pool(self):
        cands = shape_candidates([], [], [_row("E1", reserve=500_000)])
        p = build_prompt({"total_auctions": 1, "upcoming_auctions": 1,
                          "generated_at": "now"}, cands, 5)
        assert "[geo]" in p or "{city_count}" in p


class TestPromptCopyGuidance:
    def _prompt(self):
        return build_prompt({"total_auctions": 1, "upcoming_auctions": 1,
                             "generated_at": "now"},
                            shape_candidates([_row("A1")], [], []), 5)

    def test_voice_does_not_license_flat_hooks(self):
        p = self._prompt()
        assert "constrains WORDS, not TENSION" in p
        assert "failed the voice, not satisfied it" in p

    def test_mechanism_fit_warns_off_small_contrast(self):
        p = self._prompt()
        assert "MECHANISM FIT" in p and "drop_pct >= 10" in p

    def test_guess_is_a_registered_mechanism(self):
        assert "guess" in poster.HOOK_MECHANISMS
        assert "- guess:" in self._prompt()

    def test_city_context_is_fenced_in_the_prompt(self):
        """The aggregates are computed, so the model may quote them but never
        round, recompute or extend them."""
        p = self._prompt()
        assert "CITY CONTEXT" in p
        assert "QUOTE THESE FIGURES EXACTLY" in p
        assert "the city number is only the ruler" in p


class TestReelThemes:
    def _drafts(self, n):
        cands = shape_candidates([_row(f"A{i}") for i in range(1, n + 1)], [], [])
        drafts = []
        for i in range(1, n + 1):
            drafts.append({
                "auction_id": f"A{i}", "angle": "closing_soon",
                "hook_mechanism": "callout",
                "post": "reserve ₹40L.\n\nbody. auctionscope.in",
                "pinned_comment": "details: auctionscope.in. not legal advice.",
                "hashtags": [], "needs_image": False, "image_headline": "",
                "needs_reel": True,
                "reel_hook": {"line1": "₹40L", "line2": "one plot."},
                "reel_context_lines": ["a bank set the date", "chennai"],
                "engagement_question": "would you bid?",
                "save_line": "", "source": cands[i - 1],
            })
        return drafts

    def test_themes_alternate_across_the_batch(self, tmp_path):
        from marketing_agents.poster import write_reel_islands
        manifest = write_reel_islands(tmp_path, {}, self._drafts(3))
        deal_rows = [r for r in manifest if r["auction_id"] != "stats"]
        themes = [json.loads((tmp_path / r["data"]).read_text())["theme"]
                  for r in deal_rows]
        assert themes == ["dark", "light", "dark"]

    def test_stats_island_stays_dark(self):
        stats = {"total_auctions": 10, "upcoming_auctions": 5, "generated_at": "x"}
        cands = shape_candidates([_row("A1")], [], [])
        island = stats_reel_island(stats, [{"auction_id": "A1", "source": cands[0]}])
        assert island["theme"] == "dark"
