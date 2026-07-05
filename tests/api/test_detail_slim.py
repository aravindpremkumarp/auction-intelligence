"""Tests for slim_detail_for_llm — the model-facing filter on
get_auction_detail payloads. Conditional by design: a field is dropped only
when its information provably survives elsewhere in the same payload. The
REST /properties detail endpoint never goes through this filter."""
from __future__ import annotations

from api.tools.cypher_tools import slim_detail_for_llm


def _detail(**fields) -> dict:
    base = {
        "auction_id": "750879",
        "fields": {
            "auction_id": "750879",
            "title": "Canara Bank Flat Auction",
            "description": "3BHK flat, Balaraman Nagar, east-facing.",
            "reserve_price_num": 2950000.0,
            "emd_num": 295000.0,
            "downloads_list": ["downloads/live_properties/750879_1.pdf"],
            "downloads_complete": True,
        },
        "relationships": {},
        "documents": [{"public_url": "https://r2/x.pdf"}],
        "price_history": [],
    }
    base["fields"].update(fields)
    return base


def test_pipeline_fields_always_dropped():
    out = slim_detail_for_llm(_detail())
    assert "downloads_list" not in out["fields"]
    assert "downloads_complete" not in out["fields"]
    # The clickable copies stay.
    assert out["documents"][0]["public_url"]


def test_identical_website_description_dropped():
    d = _detail(website_description="3BHK flat, Balaraman Nagar, east-facing.")
    out = slim_detail_for_llm(d)
    assert "website_description" not in out["fields"]
    assert out["fields"]["description"]  # original stays


def test_differing_website_description_kept():
    """~10% of rows genuinely differ — both copies must survive."""
    d = _detail(website_description="Different marketing copy with extras.")
    out = slim_detail_for_llm(d)
    assert out["fields"]["website_description"] == "Different marketing copy with extras."


def test_raw_prices_dropped_only_when_numeric_twin_exists():
    d = _detail(reserve_price_raw="₹29,50,000.00", emd_raw="₹2,95,000.00")
    out = slim_detail_for_llm(d)
    assert "reserve_price_raw" not in out["fields"]
    assert "emd_raw" not in out["fields"]

    # Numeric parse failed -> the raw string is the only price info. Keep it.
    d2 = _detail(reserve_price_raw="Rs. 29.5 Lakhs (approx)", reserve_price_num=None)
    out2 = slim_detail_for_llm(d2)
    assert out2["fields"]["reserve_price_raw"] == "Rs. 29.5 Lakhs (approx)"


def test_none_and_malformed_pass_through():
    assert slim_detail_for_llm(None) is None
    assert slim_detail_for_llm({"auction_id": "x"}) == {"auction_id": "x"}
