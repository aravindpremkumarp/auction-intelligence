"""Unit tests for the per-property OG card feature (no network, no browser).

Two halves:
  * scripts/generate_property_og.py — a /properties row becomes a grounded
    #data island (state, locality, price-drop), and a stable R2 key.
  * scripts/prerender_properties.py — that card's URL reaches og:image,
    twitter:image and the JSON-LD `image` on BOTH schema branches, with a safe
    fallback to the generic site card when a property has no card yet.

Run: pytest tests/scripts -q
"""

import json
import re
from pathlib import Path

import pytest

from scripts.generate_property_og import build_island, is_ended, og_key
from scripts.prerender_properties import (
    DEFAULT_OG_IMAGE,
    load_og_manifest,
    og_image_for,
    render_page,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FUTURE = "2027-07-16T11:30:00Z"
PAST = "2020-01-01T10:00:00Z"


def _row(**kw):
    """A /properties row — the shape iter_all_live_candidates yields."""
    row = {
        "auction_id": "798444",
        "title": "South Indian Bank Land Auction in Manmangalam, Karur",
        "city": "Karur",
        "area": "Manmangalam Taluk",
        "bank": "South Indian Bank",
        "bank_short": "South Indian Bank",
        "property_types": ["Land"],
        "asset_category": "Residential",
        "reserve_price": 3100000.0,
        "previous_reserve_price": None,
        "auction_start": FUTURE,
    }
    row.update(kw)
    return row


class TestBuildIsland:
    """The card carries no authored copy — every field is off the record.
    These pin that the derived bits (state, locality, drop) stay honest."""

    def test_live_auction(self):
        island = build_island(_row())
        assert island["state"] == "live"
        assert island["eyebrow"] == "Live auction"
        assert island["asset_type"] == "Residential Land"
        assert island["city"] == "Karur"
        assert island["reserve_price"] == 3100000.0
        assert island["auction_date"] == "16 Jul 2027"
        assert island["previous_reserve_price"] is None

    def test_price_drop_state_when_earlier_reserve_is_higher(self):
        island = build_island(_row(previous_reserve_price=3187000.0))
        assert island["state"] == "price_drop"
        assert island["eyebrow"] == "Price drop"
        assert island["previous_reserve_price"] == 3187000.0
        assert "earlier listing vs current re-auction" in island["source_line"]

    def test_earlier_reserve_not_higher_is_cleared_not_shown(self):
        # A re-auction that went UP (or stayed flat) is not a "drop" — the
        # template must not draw a strike-through or a negative percentage.
        for prev in (3100000.0, 2900000.0):
            island = build_island(_row(previous_reserve_price=prev))
            assert island["state"] == "live"
            assert island["previous_reserve_price"] is None

    def test_ended_auction_is_never_framed_as_biddable(self):
        island = build_island(_row(auction_start=PAST))
        assert island["state"] == "ended"
        assert island["eyebrow"] == "Auction closed"
        assert "for that round" in island["source_line"]

    def test_ended_wins_over_price_drop(self):
        island = build_island(_row(auction_start=PAST, previous_reserve_price=9_000_000.0))
        assert island["state"] == "ended"

    def test_locality_blank_when_it_just_repeats_the_city(self):
        assert build_island(_row(area="karur"))["locality"] == ""
        assert build_island(_row(area="Manmangalam Taluk"))["locality"] == "Manmangalam Taluk"

    def test_no_reserve_price_renders_no_card(self):
        # The number IS the card; we don't publish one with a blank where the
        # price goes.
        assert build_island(_row(reserve_price=None)) is None
        assert build_island(_row(reserve_price=0)) is None

    def test_unparseable_date_blanks_rather_than_guessing(self):
        assert build_island(_row(auction_start="garbage"))["auction_date"] == ""

    def test_bank_falls_back_and_blanks_cleanly(self):
        assert build_island(_row(bank_short="", bank="Canara Bank"))["bank"] == "Canara Bank"
        assert build_island(_row(bank_short="", bank=""))["bank"] == ""


class TestIsEnded:
    def test_past_is_ended(self):
        assert is_ended({"auction_start": PAST}) is True

    def test_future_is_not(self):
        assert is_ended({"auction_start": FUTURE}) is False

    @pytest.mark.parametrize("value", [None, "", "garbage", 12345])
    def test_unreadable_is_not_ended(self, value):
        # Defaulting to "ended" would wrongly grey out a live auction.
        assert is_ended({"auction_start": value}) is False


class TestOgKey:
    def test_is_deterministic(self):
        assert og_key("798444") == og_key("798444") == "property-og/798444.png"

    def test_strips_path_unsafe_characters(self):
        assert og_key("../../etc/passwd") == "property-og/etcpasswd.png"

    def test_accepts_non_string_ids(self):
        assert og_key(798444) == "property-og/798444.png"


class TestOgImageFor:
    MANIFEST = {"798444": "https://pub-abc.r2.dev/property-og/798444.png"}

    def test_property_with_a_card_gets_its_own(self):
        assert og_image_for("798444", self.MANIFEST) == self.MANIFEST["798444"]

    def test_property_without_one_falls_back_to_the_site_card(self):
        # Never leave a page pointing at a 404 image.
        assert og_image_for("999999", self.MANIFEST) == DEFAULT_OG_IMAGE

    def test_empty_or_missing_manifest_falls_back(self):
        assert og_image_for("798444", {}) == DEFAULT_OG_IMAGE
        assert og_image_for("798444", None) == DEFAULT_OG_IMAGE

    @pytest.mark.parametrize("bad", ["", "not-a-url", "/relative/path.png", 42, None])
    def test_junk_manifest_values_fall_back(self, bad):
        assert og_image_for("798444", {"798444": bad}) == DEFAULT_OG_IMAGE


class TestLoadOgManifest:
    def test_missing_file_is_not_an_error(self):
        assert load_og_manifest(Path("/nonexistent/og-manifest.json")) == {}

    def test_malformed_json_is_not_an_error(self, tmp_path):
        p = tmp_path / "og-manifest.json"
        p.write_text("{not json", encoding="utf-8")
        assert load_og_manifest(p) == {}

    def test_non_dict_json_is_rejected(self, tmp_path):
        p = tmp_path / "og-manifest.json"
        p.write_text('["a", "b"]', encoding="utf-8")
        assert load_og_manifest(p) == {}

    def test_reads_a_real_manifest(self, tmp_path):
        p = tmp_path / "og-manifest.json"
        p.write_text('{"798444": "https://x/og.png"}', encoding="utf-8")
        assert load_og_manifest(p) == {"798444": "https://x/og.png"}


class TestRenderPageOgImage:
    """The bug this feature exists to fix: 664 pages all declaring the same
    picture, in the meta tags AND in the Product/RealEstateListing JSON-LD."""

    CARD = "https://pub-abc.r2.dev/property-og/798444.png"

    @staticmethod
    def _shell() -> str:
        return (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")

    @staticmethod
    def _fields(ended: bool = False):
        return {
            "title": "South Indian Bank Land Auction, Karur",
            "reserve_price_num": 3100000.0, "emd_num": 310000.0,
            "auction_start_dt": PAST if ended else FUTURE,
            "description": "x" * 200, "district": "Karur",
        }

    REL = {"city": {"name": "Karur"}, "area": {"name": "Manmangalam"},
           "bank": {"name": "South Indian Bank"}, "property_types": ["Land"]}

    def _render(self, og_image, ended=False):
        return render_page(self._shell(), "798444", self._fields(ended), self.REL, og_image)

    @staticmethod
    def _meta(page, pattern):
        m = re.search(pattern, page)
        return m.group(1) if m else None

    @staticmethod
    def _jsonld(page):
        return [json.loads(b) for b in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', page, re.S)]

    def test_meta_tags_get_the_property_card(self):
        page = self._render(self.CARD)
        assert self._meta(page, r'<meta property="og:image" content="([^"]*)"') == self.CARD
        assert self._meta(page, r'<meta name="twitter:image" content="([^"]*)"') == self.CARD

    def test_shell_dimensions_already_match_the_card_so_stay_put(self):
        page = self._render(self.CARD)
        assert self._meta(page, r'og:image:width" content="([^"]*)"') == "1200"
        assert self._meta(page, r'og:image:height" content="([^"]*)"') == "630"

    def test_product_jsonld_image_is_the_property_not_the_logo(self):
        blocks = self._jsonld(self._render(self.CARD))
        product = next(b for b in blocks if b.get("@type") == "Product")
        assert product["image"] == self.CARD

    def test_ended_listing_jsonld_also_carries_an_image(self):
        blocks = self._jsonld(self._render(self.CARD, ended=True))
        listing = next(b for b in blocks if b.get("@type") == "RealEstateListing")
        assert listing["image"] == self.CARD

    def test_default_keeps_the_previous_behaviour(self):
        # Callers that pass nothing (and pages with no card yet) still emit a
        # valid image rather than an empty attribute.
        page = render_page(self._shell(), "798444", self._fields(), self.REL)
        assert self._meta(page, r'<meta property="og:image" content="([^"]*)"') == DEFAULT_OG_IMAGE
        product = next(b for b in self._jsonld(page) if b.get("@type") == "Product")
        assert product["image"] == DEFAULT_OG_IMAGE

    def test_only_the_image_tags_move(self):
        # og:title / og:description / canonical are set elsewhere in render_page;
        # this guards against a greedy regex eating a neighbouring tag.
        page = self._render(self.CARD)
        assert page.count('property="og:image"') == 1
        assert page.count('name="twitter:image"') == 1
        assert 'property="og:title"' in page and 'rel="canonical"' in page
