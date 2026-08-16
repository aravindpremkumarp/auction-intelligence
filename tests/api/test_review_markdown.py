"""Smoke + behavior tests for the markdown review endpoints."""
from __future__ import annotations
from datetime import datetime, timezone

import pytest


def _admin_header() -> dict[str, str]:
    from tests.api.conftest import auth_header  # type: ignore
    return auth_header(sub="admin-sub", email="admin@example.com")


def _ensure_admin_user() -> None:
    from api.neo4j_client import _users  # type: ignore[attr-defined]
    _users["admin-sub"] = {
        "supabase_id": "admin-sub",
        "email": "admin@example.com",
        "name": "Admin",
        "role": "admin",
        "enabled": True,
        "created_at": datetime.now(timezone.utc),
        "last_login_at": datetime.now(timezone.utc),
    }


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from api.main import app
    return TestClient(app)


def test_markdown_accepts_uniform_status_values(client) -> None:
    _ensure_admin_user()
    for s in ("pending", "verified", "edited", "all"):
        r = client.get(f"/review/markdown?status={s}", headers=_admin_header())
        assert r.status_code == 200, f"status={s} rejected: {r.text}"


def test_markdown_accepts_score_max(client) -> None:
    _ensure_admin_user()
    r = client.get(
        "/review/markdown?score_min=50&score_max=80",
        headers=_admin_header(),
    )
    assert r.status_code == 200


def test_markdown_accepts_notice_type(client) -> None:
    _ensure_admin_user()
    for nt in ("all", "single", "multi", "unclassified"):
        r = client.get(f"/review/markdown?notice_type={nt}", headers=_admin_header())
        assert r.status_code == 200, f"notice_type={nt} rejected: {r.text}"


def test_markdown_stats_includes_edited(client) -> None:
    _ensure_admin_user()
    r = client.get("/review/markdown/stats", headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert "edited" in body
    assert "verified" in body
    assert "pending" in body
    assert "total" in body


def test_markdown_by_property_routes_registered(client) -> None:
    from api.main import app
    paths = {r.path for r in app.routes}
    assert "/review/markdown/by-property" in paths


def test_markdown_by_property_returns_empty(client) -> None:
    _ensure_admin_user()
    r = client.get("/review/markdown/by-property", headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["rows"] == []


def test_markdown_rejects_legacy_status(client) -> None:
    _ensure_admin_user()
    for s in ("good", "bad", "unscored"):
        r = client.get(f"/review/markdown?status={s}", headers=_admin_header())
        assert r.status_code == 422, f"status={s} should be rejected"


def test_markdown_row_model_exposes_highlights() -> None:
    # Regression: the highlight spans must survive the response_model. FastAPI
    # silently drops fields not declared on MarkdownRow, which is what hid the
    # highlights from the UI.
    from api.review.router import MarkdownRow
    assert "highlights" in MarkdownRow.model_fields
    row = MarkdownRow(filename="n.jpg", highlights=[{"start": 3, "end": 9}])
    hl = row.model_dump()["highlights"]
    assert len(hl) == 1 and hl[0]["start"] == 3 and hl[0]["end"] == 9
    # default is an empty list, never missing
    assert MarkdownRow(filename="n.jpg").model_dump()["highlights"] == []


def test_markdown_row_model_exposes_ocr_health() -> None:
    # Regression: list_markdown_queue returns ocr_health_score/ocr_health_flags,
    # but if MarkdownRow doesn't declare them FastAPI strips them from the JSON,
    # so the review UI's healthPill() never sees a flag and the `table-collapse`
    # pill can't render. Same failure mode as the highlights bug above.
    from api.review.router import MarkdownRow
    assert "ocr_health_score" in MarkdownRow.model_fields
    assert "ocr_health_flags" in MarkdownRow.model_fields
    row = MarkdownRow(filename="n.jpg", ocr_health_score=65,
                      ocr_health_flags=["table-collapse"])
    dumped = row.model_dump()
    assert dumped["ocr_health_score"] == 65
    assert dumped["ocr_health_flags"] == ["table-collapse"]
    # absent/unscored stays null, not missing
    bare = MarkdownRow(filename="n.jpg").model_dump()
    assert bare["ocr_health_score"] is None
    assert bare["ocr_health_flags"] is None


def test_markdown_property_row_model_exposes_ocr_health() -> None:
    # The by-property markdown table shows OCR-health as its score too, so the
    # fields must survive MarkdownPropertyRow's response_model.
    from api.review.router import MarkdownPropertyRow
    assert "ocr_health_score" in MarkdownPropertyRow.model_fields
    assert "ocr_health_flags" in MarkdownPropertyRow.model_fields
    row = MarkdownPropertyRow(auction_id="a1", ocr_health_score=65,
                              ocr_health_flags=["table-collapse"])
    dumped = row.model_dump()
    assert dumped["ocr_health_score"] == 65
    assert dumped["ocr_health_flags"] == ["table-collapse"]


def test_markdown_accepts_parse_quality_bounds(client) -> None:
    _ensure_admin_user()
    for path in ("/review/markdown", "/review/markdown/by-property"):
        r = client.get(f"{path}?pq_min=1&pq_max=3.5", headers=_admin_header())
        assert r.status_code == 200, f"{path} rejected pq bounds: {r.text}"


def test_markdown_rejects_parse_quality_above_scale(client) -> None:
    # Parse quality is 0–5, not 0–100 — a 100 here would be an OCR-health value
    # pasted into the wrong filter and must not silently match everything.
    _ensure_admin_user()
    r = client.get("/review/markdown?pq_min=100", headers=_admin_header())
    assert r.status_code == 422


def test_markdown_row_models_expose_parse_quality() -> None:
    # Same response_model trap as ocr_health above: undeclared fields are
    # stripped, and the UI's parse pill / filter would silently show nothing.
    from api.review.router import BlocksDoc, MarkdownPropertyRow, MarkdownRow
    for model, kwargs in ((MarkdownRow, {"filename": "n.jpg"}),
                          (MarkdownPropertyRow, {"auction_id": "a1"}),
                          (BlocksDoc, {"filename": "n.jpg"})):
        assert "parse_quality_score" in model.model_fields, model.__name__
        assert model(**kwargs).model_dump()["parse_quality_score"] is None
        # Fractional scores must survive as floats, not truncate to int.
        assert model(**kwargs, parse_quality_score=3.5).model_dump()[
            "parse_quality_score"] == 3.5


def test_row_models_expose_ink_uncovered_ratio() -> None:
    # Feeds the health pill's "N% of the page's ink was never read" tooltip;
    # undeclared here it would be stripped and the pill could only say that
    # something was dropped, not how much.
    from api.review.router import BlocksDoc, MarkdownRow
    for model, kwargs in ((MarkdownRow, {"filename": "n.jpg"}),
                          (BlocksDoc, {"filename": "n.jpg"})):
        assert "ink_uncovered_ratio" in model.model_fields, model.__name__
        assert model(**kwargs).model_dump()["ink_uncovered_ratio"] is None
        assert model(**kwargs, ink_uncovered_ratio=0.3834).model_dump()[
            "ink_uncovered_ratio"] == 0.3834


def test_bulk_confirm_carries_parse_quality_bounds(monkeypatch, client) -> None:
    # The button is labelled with the parse-quality-filtered queue's count, so
    # the bounds must reach auto_confirm_markdown — otherwise bulk-confirm
    # verifies documents the reviewer never saw.
    _ensure_admin_user()
    import api.review.queries as queries

    seen: dict = {}

    def fake_auto_confirm(**kwargs):
        seen.update(kwargs)
        return {"count": 0, "dry_run": True}

    monkeypatch.setattr(queries, "auto_confirm_markdown", fake_auto_confirm)
    r = client.post(
        "/review/markdown/bulk-confirm",
        json={"score_min": 0, "score_max": 100, "pq_min": 2, "pq_max": 4,
              "dry_run": True},
        headers=_admin_header(),
    )
    assert r.status_code == 200, r.text
    assert seen["pq_min"] == 2.0
    assert seen["pq_max"] == 4.0


def test_markdown_accepts_health_flag_filter(client) -> None:
    _ensure_admin_user()
    for path in ("/review/markdown", "/review/markdown/by-property"):
        r = client.get(f"{path}?flags=missing-region&flags=repetition",
                       headers=_admin_header())
        assert r.status_code == 200, f"{path} rejected flags: {r.text}"


def test_markdown_rejects_unknown_health_flag(client) -> None:
    # A typo'd flag would match nothing and read as "no failures of this kind",
    # which is worse than an error.
    _ensure_admin_user()
    r = client.get("/review/markdown?flags=missing-regions", headers=_admin_header())
    assert r.status_code == 422
    assert "missing-regions" in r.text


def test_clean_health_flags_dedupes_into_severity_order() -> None:
    from api.review.router import _clean_health_flags
    from pipeline.ocr_health import HEALTH_FLAGS

    assert _clean_health_flags(None) is None
    assert _clean_health_flags([]) is None
    assert _clean_health_flags(["repetition", "missing-region", "repetition"]) == [
        "missing-region", "repetition"]
    # Every emitted flag must be selectable, or a failure mode becomes invisible.
    assert _clean_health_flags(list(HEALTH_FLAGS)) == list(HEALTH_FLAGS)


def test_health_flags_where_matches_any_selected_flag() -> None:
    from api.review.queries import _health_flags_where
    where, params = _health_flags_where(None)
    assert where == [] and params == {}
    where, params = _health_flags_where(["missing-region", "repetition"])
    assert where == [
        "any(f IN coalesce(d.ocr_health_flags, []) WHERE f IN $health_flags)"]
    assert params == {"health_flags": ["missing-region", "repetition"]}


def test_bulk_confirm_carries_health_flags(monkeypatch, client) -> None:
    # Same alignment rule as the score bounds: the button's count comes from the
    # flag-filtered queue, so the action must be flag-filtered too.
    _ensure_admin_user()
    import api.review.queries as queries

    seen: dict = {}
    monkeypatch.setattr(queries, "auto_confirm_markdown",
                        lambda **kw: (seen.update(kw), {"count": 0, "dry_run": True})[1])
    r = client.post(
        "/review/markdown/bulk-confirm",
        json={"score_min": 0, "score_max": 100,
              "flags": ["missing-region"], "dry_run": True},
        headers=_admin_header(),
    )
    assert r.status_code == 200, r.text
    assert seen["flags"] == ["missing-region"]


def test_parse_quality_where_requires_a_stored_score() -> None:
    # An unscored Document means "never measured", not "fine" — it must drop out
    # of the queue once the reviewer sets either bound.
    from api.review.queries import _parse_quality_where
    where, params = _parse_quality_where(None, None)
    assert where == [] and params == {}
    where, params = _parse_quality_where(2.0, 4.0)
    assert "d.parse_quality_score IS NOT NULL" in where
    assert "d.parse_quality_score >= $pq_min" in where
    assert "d.parse_quality_score <= $pq_max" in where
    assert params == {"pq_min": 2.0, "pq_max": 4.0}


def test_block_model_exposes_health() -> None:
    # get_blocks attaches a read-time per-block health verdict; Block must
    # declare it or FastAPI strips it (same bug class as the highlights guard).
    from api.review.router import Block
    assert "health" in Block.model_fields
    blk = Block(id="b1", bbox=[0, 0, 1, 1], label="Text",
                health={"score": 60, "flags": ["repetition"]})
    dumped = blk.model_dump()
    assert dumped["health"]["flags"] == ["repetition"]
    assert dumped["health"]["score"] == 60
    # absent health stays None, never missing
    bare = Block(id="b2", bbox=[0, 0, 1, 1], label="Text").model_dump()
    assert bare["health"] is None


def test_block_auto_reextract_marker_and_flag() -> None:
    # One-time auto-fix: the request carries an `auto` flag and the block
    # persists an `auto_reextract_at` marker so it never re-fires.
    from api.review.router import Block, ReExtractBody
    assert ReExtractBody(auto=True).auto is True
    assert ReExtractBody().auto is False           # defaults off
    assert "auto_reextract_at" in Block.model_fields
    b = Block(id="b1", bbox=[0, 0, 1, 1], label="Text",
              auto_reextract_at="2026-07-20T18:00:00+00:00")
    assert b.model_dump()["auto_reextract_at"] == "2026-07-20T18:00:00+00:00"
    assert Block(id="b2", bbox=[0, 0, 1, 1], label="Text"
                 ).model_dump()["auto_reextract_at"] is None


def test_pipeline_overview_endpoint(client) -> None:
    # Call it rather than introspecting app.routes: route objects differ across
    # FastAPI versions, and a request proves registration on any of them.
    _ensure_admin_user()
    r = client.get("/review/pipeline", headers=_admin_header())
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("stages", "upcoming_stages", "flags", "extraction_pending",
                "unmeasured"):
        assert key in body, key


def test_pipeline_overview_model_defaults() -> None:
    # The dashboard must render before any counter has data.
    from api.review.router import PipelineOverview
    blank = PipelineOverview(stages=[], upcoming_stages=[], flags=[]).model_dump()
    assert blank["extraction_pending"] == 0 and blank["unmeasured"] == 0


def test_pipeline_stage_counts_are_cumulative() -> None:
    # A funnel only means something if each stage is a subset of the one before.
    # Counted independently the corpus reports more documents extracted (1,553)
    # than markdown-verified (1,489), because extraction ran on unverified
    # notices — so every "drop" would be fiction.
    from api.review import queries as q

    captured: dict = {}

    def fake_read(cypher, params=None, **kw):
        captured["cypher"] = cypher
        return [{k: 0 for k, _l, _p in q.PIPELINE_STAGES}]

    orig = q.run_read_query
    q.run_read_query = fake_read
    try:
        stages = q._stage_counts("MATCH (d:Document)", "", {})
    finally:
        q.run_read_query = orig

    built = [s for s in stages if not s["planned"]]
    planned = [s for s in stages if s["planned"]]
    assert [s["key"] for s in built] == [k for k, _l, _p in q.PIPELINE_STAGES]
    assert [s["key"] for s in planned] == [k for k, _l in q.PIPELINE_PLANNED]
    # A stage that does not exist reports no count. A 0 would read as "built,
    # nothing reached it" — a different and misleading fact.
    assert all(s["count"] is None for s in planned)
    assert all(isinstance(s["count"], int) for s in built)
    cypher = captured["cypher"]
    # The last stage's CASE must carry every earlier predicate.
    for _k, _l, pred in q.PIPELINE_STAGES:
        assert f"({pred})" in cypher, pred
    assert cypher.count("d.markdown_verified_at IS NOT NULL") >= 3


def test_pipeline_stage_detail_endpoint(client) -> None:
    _ensure_admin_user()
    r = client.get("/review/pipeline/extracted", headers=_admin_header())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["key"] == "extracted" and body["label"]
    for panel in body["panels"]:
        assert "title" in panel and isinstance(panel["rows"], list)


def test_pipeline_stage_detail_rejects_unknown_stage(client) -> None:
    # A typo'd stage must 404 rather than render an empty page that reads as
    # "this stage has no data".
    _ensure_admin_user()
    r = client.get("/review/pipeline/not-a-stage", headers=_admin_header())
    assert r.status_code == 404


def test_planned_stage_detail_says_so(client) -> None:
    _ensure_admin_user()
    r = client.get("/review/pipeline/entity_resolution", headers=_admin_header())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["panels"] and body["panels"][0]["rows"] == []
    assert "planned" in body["panels"][0]["note"]
