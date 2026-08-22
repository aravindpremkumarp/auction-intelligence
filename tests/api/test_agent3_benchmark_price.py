"""Tests for api/agent3/benchmark_price.py.

The refusals matter more than the numbers here: this tool declines ~70% of
listings by design, and each refusal must name a real limit of the data
rather than read as a failure.
"""
from __future__ import annotations

from api.agent3 import benchmark_price as BP


def _subject(lot_count=1, sqft=714.0, price=4641000.0, **kw):
    base = {"auction_id": "748779", "reserve_price": price,
            "lot_count": lot_count, "sqft": sqft, "area": "Some Area",
            "city": "Coimbatore", "district": "Coimbatore",
            "property_types": ["Land And Building"]}
    base.update(kw)
    return base


def _stub(monkeypatch, subject=None, rings=None):
    """First query is the subject; later ones are rings, in order."""
    queue = list(rings or [])

    def fake(cypher, params=None, timeout=10.0, max_rows=200):
        if "lot_count, sqft" in cypher or "AS lot_count" in cypher and "percentileCont" not in cypher:
            return [subject] if subject else []
        return [queue.pop(0)] if queue else [{"n": 0}]

    monkeypatch.setattr(BP, "run_read_query", fake)


def _ring(n=20, median=3000, p25=2000, p75=4000, below=15):
    return {"n": n, "median": median, "p25": p25, "p75": p75,
            "below_subject": below}


# ── the refusals ─────────────────────────────────────────────────────────

def test_multi_lot_notice_is_refused_with_the_reason(monkeypatch):
    """Reserve price is on the listing, extent on the lot. With several lots
    nothing says which lot the price refers to — dividing would invent a
    number. This is ~70% of listings."""
    _stub(monkeypatch, subject=_subject(lot_count=4))
    out = BP.benchmark_price("744314")
    assert out["priced"] is False
    assert "4 lots" in out["reason"]
    assert "made-up number" in out["reason"]


def test_missing_reserve_price_is_refused(monkeypatch):
    _stub(monkeypatch, subject=_subject(price=None))
    out = BP.benchmark_price("X")
    assert out["priced"] is False and "no reserve price" in out["reason"]


def test_missing_extent_is_refused(monkeypatch):
    _stub(monkeypatch, subject=_subject(sqft=None))
    out = BP.benchmark_price("X")
    assert out["priced"] is False and "no extent" in out["reason"]


def test_no_lot_at_all_is_refused(monkeypatch):
    _stub(monkeypatch, subject=_subject(lot_count=0))
    out = BP.benchmark_price("X")
    assert out["priced"] is False and "no extent to divide" in out["reason"]


def test_absurd_price_per_sqft_is_refused(monkeypatch):
    """A 1.6-sqft extent produced ₹8,387,097/sqft in the live corpus. The
    extent or the price is wrong, so the comparison would be too."""
    _stub(monkeypatch, subject=_subject(sqft=200.0, price=50_000_000_000.0))
    out = BP.benchmark_price("X")
    assert out["priced"] is False and "plausible" in out["reason"]


def test_unknown_id_is_refused(monkeypatch):
    _stub(monkeypatch, subject=None)
    out = BP.benchmark_price("NOPE")
    assert out["priced"] is False
    assert "No listing carries" in out["reason"]


# ── the numbers ──────────────────────────────────────────────────────────

def test_a_priceable_listing_reports_per_sqft_and_percentile(monkeypatch):
    _stub(monkeypatch, subject=_subject(),
          rings=[_ring(n=30, median=2946, below=26)])
    out = BP.benchmark_price("748779")
    assert out["priced"] is True
    assert out["subject"]["reserve_per_sqft"] == 6500.0
    assert out["comparisons"][0]["subject_percentile"] == 87


def test_thin_rings_are_reported_not_summarised(monkeypatch):
    """A percentile off three comparables is noise wearing a number's
    clothes. Area rings are thin for most places — 36 of 417."""
    _stub(monkeypatch, subject=_subject(),
          rings=[_ring(n=1), _ring(n=30)])
    out = BP.benchmark_price("748779")
    thin = {t["ring"] for t in out["rings_too_thin"]}
    assert "same area" in thin
    assert all(c["comparables"] >= BP.MIN_COMPARABLES for c in out["comparisons"])


def test_no_viable_ring_means_not_priced(monkeypatch):
    _stub(monkeypatch, subject=_subject(), rings=[_ring(n=1)] * 4)
    out = BP.benchmark_price("748779")
    assert out["priced"] is False
    assert "comparables to judge it against" in out["reason"]


# ── the caveat that must never be dropped ────────────────────────────────

def test_every_response_carries_the_not_market_value_basis(monkeypatch):
    """The graph has NO sold prices. If this caveat can be dropped in
    summarising, the tool becomes a valuation engine it has no right to be."""
    for subject in (_subject(), _subject(lot_count=5), None):
        _stub(monkeypatch, subject=subject, rings=[_ring()])
        out = BP.benchmark_price("X")
        assert "not market value" in out["basis"]
        assert "NO sold prices" in out["basis"]


def test_the_pricing_sqft_floor_is_stricter_than_the_search_floor():
    """The 1-sqft floor used for filtering lets parse errors into a DIVISION,
    where they explode. Raising it to 100 for pricing dropped the corpus
    maximum from ₹8,387,097/sqft to ₹229,358."""
    from api.agent3.common import SQFT_FLOOR

    assert BP.PRICING_SQFT_FLOOR > SQFT_FLOOR
    assert BP.PRICING_SQFT_FLOOR == 100.0
