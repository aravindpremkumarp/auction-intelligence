"""The freshness loop for prerendered property pages.

The generators walk live inventory only, so a page written while its auction was
open is never revisited once that auction closes — it keeps the live wording and
an InStock Offer. `--refresh` is the only path that fixes that, and these cover
the pieces it turns on: finding the pages already on disk, recognising the ones
already framed closed, and re-rendering a closed round without a live Offer.
"""
from __future__ import annotations

import json

from scripts.prerender_properties import (
    CLOSED_MARKER, is_ended, iter_existing_pages, page_is_closed, render_page,
)

TEMPLATE = (
    "<html><head><title>x</title>"
    '<meta name="description" content="x">'
    '<link rel="canonical" href="x">'
    '<meta property="og:title" content="x">'
    '<meta property="og:description" content="x">'
    '<meta property="og:url" content="x">'
    '<meta property="og:image" content="x">'
    '<meta name="twitter:title" content="x">'
    '<meta name="twitter:description" content="x">'
    '<meta name="twitter:image" content="x">'
    "</head>\n<body>\n</body></html>"
)


def _page(tmp_path, auction_id: str, body: str) -> None:
    d = tmp_path / auction_id
    d.mkdir(parents=True)
    (d / "index.html").write_text(body, encoding="utf-8")


def _fields(start: str, description: str = "x" * 200) -> dict:
    return {
        "auction_start_dt": start,
        "application_deadline_dt": start,
        "description": description,
        "reserve_price_num": 5_000_000.0,
        "property_type": "Plot",
    }


def test_iter_existing_pages_finds_pages_on_disk_sorted(tmp_path):
    _page(tmp_path, "900002", "b")
    _page(tmp_path, "900001", "a")
    (tmp_path / "900003").mkdir()  # a directory with no index.html is not a page
    (tmp_path / "loose.html").write_text("x", encoding="utf-8")

    assert [aid for aid, _ in iter_existing_pages(tmp_path)] == ["900001", "900002"]


def test_iter_existing_pages_tolerates_a_missing_tree(tmp_path):
    assert list(iter_existing_pages(tmp_path / "nope")) == []


def test_page_is_closed_reads_the_ended_framing(tmp_path):
    _page(tmp_path, "900001", f"<p>{CLOSED_MARKER} — kept for reference</p>")
    _page(tmp_path, "900002", "<p>deadline: 24 Jul 2026</p>")

    assert page_is_closed(tmp_path / "900001" / "index.html") is True
    assert page_is_closed(tmp_path / "900002" / "index.html") is False
    assert page_is_closed(tmp_path / "900003" / "index.html") is False


def test_is_ended_turns_on_the_auction_start_date():
    assert is_ended(_fields("2020-01-01T11:00:00+00:00")) is True
    assert is_ended(_fields("2099-01-01T11:00:00+00:00")) is False


def _jsonld(page: str) -> list[dict]:
    import re
    return [json.loads(m) for m in
            re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)]


def test_a_closed_auction_renders_without_a_live_offer():
    page = render_page(TEMPLATE, "900001", _fields("2020-01-01T11:00:00+00:00"), {})

    assert CLOSED_MARKER in page
    blocks = _jsonld(page)
    assert blocks, "expected JSON-LD on the page"
    assert not any("offers" in b for b in blocks), \
        "a closed round must not advertise an Offer"


def test_a_live_auction_still_renders_its_offer():
    page = render_page(TEMPLATE, "900001", _fields("2099-01-01T11:00:00+00:00"), {})

    assert CLOSED_MARKER not in page
    offers = [b["offers"] for b in _jsonld(page) if "offers" in b]
    assert offers, "a live auction should carry an Offer"
    assert offers[0]["availability"] == "https://schema.org/InStock"


def test_re_rendering_a_page_whose_auction_closed_changes_it():
    """The whole point of the refresh pass: same auction id, same template, but
    the page it produces once the date passes is not the page on disk."""
    live = render_page(TEMPLATE, "900001", _fields("2099-01-01T11:00:00+00:00"), {})
    closed = render_page(TEMPLATE, "900001", _fields("2020-01-01T11:00:00+00:00"), {})

    assert live != closed
    assert CLOSED_MARKER in closed and CLOSED_MARKER not in live


# --- Offer availability window -------------------------------------------------
# Registration closes BEFORE bidding opens, so keying the Offer's end to
# application_deadline_dt produced a window that ended before it started and a
# priceValidUntil already in the past on auctions that had not yet happened.

def _live_fields(**over) -> dict:
    f = {
        "auction_start_dt": "2099-06-10T10:30:00+00:00",
        "auction_end_dt": "2099-06-10T13:30:00+00:00",
        "application_deadline_dt": "2099-06-09T17:00:00+00:00",
        "description": "x" * 200,
        "reserve_price_num": 5_000_000.0,
    }
    f.update(over)
    return f


def _offer(fields: dict) -> dict:
    page = render_page(TEMPLATE, "900001", fields, {})
    offers = [b["offers"] for b in _jsonld(page) if "offers" in b]
    assert offers, "expected a live Offer"
    return offers[0]


def test_offer_window_tracks_bidding_not_the_paperwork_deadline():
    offer = _offer(_live_fields())

    assert offer["availabilityStarts"] == "2099-06-10"
    assert offer["availabilityEnds"] == "2099-06-10"
    assert offer["priceValidUntil"] == "2099-06-10"


def test_offer_window_never_ends_before_it_starts():
    """The bug this guards: the deadline precedes the auction, so using it as
    the end produced availabilityEnds < availabilityStarts."""
    offer = _offer(_live_fields())

    assert offer["availabilityEnds"] >= offer["availabilityStarts"]
    assert offer["priceValidUntil"] >= offer["availabilityStarts"]


def test_offer_end_falls_back_to_the_start_date_without_an_end_time():
    offer = _offer(_live_fields(auction_end_dt=None))

    assert offer["priceValidUntil"] == "2099-06-10"
    assert offer["availabilityEnds"] == "2099-06-10"


def test_the_application_deadline_is_still_published_as_a_fact():
    page = render_page(TEMPLATE, "900001", _live_fields(), {})
    props = [b for b in _jsonld(page) if "additionalProperty" in b]
    names = {p["name"]: p["value"] for b in props for p in b["additionalProperty"]}

    assert "Application deadline" in names
    assert "Auction date" in names
