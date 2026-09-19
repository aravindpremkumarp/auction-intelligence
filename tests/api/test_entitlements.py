"""What a free visitor may see, pinned.

These tests exist because the failure mode is silent: `/auction/{id}` returns
`properties(a)` — every property on the node — so the day the loader writes a
new field, a denylist would publish it and nobody would notice. The allowlist
is asserted directly, and the two endpoints are driven end-to-end to prove
the redaction happens on the server rather than in the browser.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from api.auth.schemas import UserOut
from api.entitlements import (
    FREE_FIELDS, is_paid, redact_detail, redact_notice,
)

props = importlib.import_module("api.properties.router")


def _user(tier: str) -> UserOut:
    return UserOut(id="u-1", email="qa@auctionscope.in", name="X", role="user",
                   enabled=True, email_verified=True, tier=tier)


def _detail() -> dict:
    return {
        "auction_id": "a-1",
        "fields": {
            "auction_id": "a-1", "title": "Plot in Dindigul",
            "reserve_price_num": 3298000, "auction_start_dt": "2026-08-04T11:00:00Z",
            "total_area": "44 cents", "village": "Balakrishnapuram",
            "property_type_effective": "plot",
            # everything below is paid
            "emd_num": 329800, "application_deadline_dt": "2026-08-02T17:00:00Z",
            "auction_end_dt": "2026-08-04T13:00:00Z",
            "contact_details": "9876543210",
            "description": "S.No.555/7, Patta 12414, bounded on the east by ...",
            "boundary_north": "Plot No.10", "door_numbers_old": "555/7",
            "registration_sub_district": "Nagayakacampatti",
            # a field nobody has thought about yet — the allowlist must drop it
            "some_future_pipeline_field": "leaked",
        },
        "relationships": {
            "city": {"name": "Dindigul"}, "bank": {"name": "Chola"},
            "branch": {"name": "Dindigul"}, "borrower": {"name": "Devi M"},
            "property_types": ["Plot"],
        },
        "documents": [{"public_url": "https://x.test/notice.pdf"}],
        "other_listings": [{"auction_id": "b-2"}],
        "photos": [], "price_history": [], "source": "baanknet",
    }


def _notice() -> dict:
    return {
        "auction_id": "a-1", "scope": "lot", "notice_lot_count": 1,
        "notice": {
            "notice_url": "https://x.test/notice.pdf",
            "emd_accounts": [{"account_no": "123", "ifsc": "HDFC0000123"}],
            "officers": [{"name": "S. Ramesh"}],
            "contacts": [{"phone": "9876543210"}],
            "sale_terms": "As is where is ...",
        },
        "property": {
            "property_type": "plot", "headline_sqft": 570.0,
            "village": "Balakrishnapuram", "district": "Dindigul",
            "possession": {"type": "symbolic"},
            "identifiers": [{"kind": "survey_old", "value": "555/7"},
                            {"kind": "patta", "value": "12414"},
                            {"kind": "survey_new", "value": "555/7D1"}],
            "boundaries": [{"side": "north", "adjacent": "Plot No.10"}],
            "parties": [{"name": "Devi M", "role": "borrower"}],
            "loans": [{"account_no": "HL24", "outstanding": 2728520}],
            "extents": [{"kind": "total", "sqft": 570.0}],
            "schedules": [], "description": "S.No.555/7 ...",
        },
        "gaps": ["No chitta number in the notice.", "No inspection date given."],
    }


def test_paid_only_means_paid() -> None:
    assert is_paid(None) is False
    assert is_paid(_user("free")) is False
    assert is_paid(_user("paid")) is True


def test_paid_detail_is_untouched() -> None:
    d = _detail()
    assert redact_detail(d, paid=True) is d


def test_free_detail_keeps_only_the_allowlist() -> None:
    out = redact_detail(_detail(), paid=False)
    assert set(out["fields"]) <= FREE_FIELDS
    assert out["fields"]["reserve_price_num"] == 3298000
    assert out["fields"]["total_area"] == "44 cents"
    for gone in ("emd_num", "application_deadline_dt", "auction_end_dt",
                 "contact_details", "description", "boundary_north",
                 "door_numbers_old", "some_future_pipeline_field"):
        assert gone not in out["fields"], gone


def test_free_detail_drops_the_paid_relationships_and_payloads() -> None:
    out = redact_detail(_detail(), paid=False)
    assert out["relationships"]["bank"] == {"name": "Chola"}
    assert "branch" not in out["relationships"]
    assert "borrower" not in out["relationships"]
    assert "documents" not in out
    assert "other_listings" not in out
    # Counted, not listed: the count is the reason to upgrade, the URL is the
    # thing being sold.
    assert out["locked"]["document_count"] == 1
    assert out["locked"]["other_listing_count"] == 1
    assert out["locked"]["tier_required"] == "paid"


def test_free_notice_gives_counts_and_kinds_but_no_values() -> None:
    out = redact_notice(_notice(), paid=False)
    blob = repr(out)
    for secret in ("555/7", "12414", "Plot No.10", "Devi M", "HL24",
                   "HDFC0000123", "S. Ramesh", "9876543210",
                   "As is where is", "notice.pdf", "No chitta number"):
        assert secret not in blob, secret
    assert out["locked"]["counts"]["identifiers"] == 3
    assert out["locked"]["counts"]["boundaries"] == 1
    assert out["locked"]["identifier_kinds"] == ["survey_old", "patta", "survey_new"]
    assert out["locked"]["gap_count"] == 2
    assert out["locked"]["has_emd_account"] is True
    assert out["locked"]["has_officer"] is True
    assert out["locked"]["has_notice_document"] is True
    # The free preview is the size/type/place the listing already promises.
    assert out["property_preview"]["headline_sqft"] == 570.0
    assert out["property_preview"]["possession_type"] == "symbolic"


def test_free_notice_on_a_multilot_notice_previews_nothing() -> None:
    """No lot is this property's own, so borrowing a sibling's extent for the
    teaser would state a fact the notice does not support."""
    bundle = {"auction_id": "a-1", "scope": "notice", "notice_lot_count": 6,
              "notice_lots": [{"headline_sqft": 900.0}, {"headline_sqft": 2400.0}],
              "notice": {}, "gaps": []}
    out = redact_notice(bundle, paid=False)
    assert out["property_preview"] == {}
    assert out["locked"]["counts"] == {"lots": 2}


def test_paid_notice_is_untouched() -> None:
    b = _notice()
    assert redact_notice(b, paid=True) is b


# ── endpoints ─────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(props, "get_auction_detail",
                        lambda aid: _detail() if aid == "a-1" else None)
    monkeypatch.setattr(props, "get_property_detail",
                        lambda ids, depth="standard": {"properties": [_notice()]}
                        if ids == ["a-1"] else {"properties": []})
    from api.main import app
    return TestClient(app)


def test_anonymous_detail_is_redacted_by_the_server(client: TestClient) -> None:
    body = client.get("/auction/a-1").json()
    assert "emd_num" not in body["fields"]
    assert "description" not in body["fields"]
    assert body["locked"]["tier_required"] == "paid"


def test_anonymous_notice_is_redacted_by_the_server(client: TestClient) -> None:
    body = client.get("/auction/a-1/notice").json()
    assert "property" not in body
    assert "gaps" not in body
    assert body["locked"]["counts"]["identifiers"] == 3


def test_a_paid_user_gets_everything(client: TestClient,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    # Zero-arg on purpose: FastAPI reads an override's signature, so `*args`
    # would be advertised as required query parameters.
    async def _paid() -> UserOut:
        return _user("paid")

    from api.auth import dependencies as deps
    from api.main import app
    app.dependency_overrides[deps.get_optional_user] = _paid
    try:
        detail = client.get("/auction/a-1").json()
        notice = client.get("/auction/a-1/notice").json()
    finally:
        app.dependency_overrides.pop(deps.get_optional_user, None)
    assert detail["fields"]["emd_num"] == 329800
    assert "locked" not in detail
    assert notice["property"]["identifiers"][0]["value"] == "555/7"
    assert notice["gaps"]
