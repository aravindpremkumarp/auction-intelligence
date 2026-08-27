"""Whole-document re-ingest honours the reviewer's OCR engine choice.

The annotator's Re-ingest button used to be hard-wired to MinerU while the
toolbar's engine picker only governed drawn-block re-extract. MinerU's vlm
reads a fully-ruled notice as ONE giant ``<table>`` (the ``table-collapse``
health flag), so re-ingesting such a notice returned a single Table block no
matter how many times the reviewer pressed it, and Datalab — which decomposes
the same page into Title / Text / Table blocks — was unreachable from the UI.

Network-free: the engine clients, the on-disk cache and Neo4j are stubbed.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

import api.review.blocks as B


# ── engine normalization ────────────────────────────────────────────────────

def test_clean_engine_accepts_both_engines():
    assert B._clean_engine("datalab") == "datalab"
    assert B._clean_engine("mineru") == "mineru"
    assert B._clean_engine("  MinerU  ") == "mineru"


def test_clean_engine_falls_back_to_pipeline_default(monkeypatch):
    import pipeline.config as CFG
    monkeypatch.setattr(CFG, "DESCRIPTION_OCR_ENGINE", "datalab")
    assert B._clean_engine(None) == "datalab"
    assert B._clean_engine("") == "datalab"
    assert B._clean_engine("gpt-vision") == "datalab"


def test_clean_engine_never_returns_an_unknown_default(monkeypatch):
    """A typo'd env var must not send an unroutable engine into the pipeline."""
    import pipeline.config as CFG
    monkeypatch.setattr(CFG, "DESCRIPTION_OCR_ENGINE", "nonsense")
    assert B._clean_engine(None) in B.REINGEST_ENGINES


# ── persist records which engine ran ────────────────────────────────────────

def _capture_run_query(monkeypatch):
    captured: dict = {}

    def _capture(cypher, params=None):
        captured["cypher"] = cypher
        captured["params"] = params
        return [{"rev": 1}]

    monkeypatch.setattr(B, "run_query", _capture)
    return captured


def test_persist_defaults_to_mineru_provenance(monkeypatch):
    captured = _capture_run_query(monkeypatch)
    B._persist_reingest_result("n.jpg", markdown="MD", blocks_json="{}",
                               markdown_raw=None, blocks_raw=None)
    assert captured["params"]["markdown_source"] == "mineru"
    assert captured["params"]["markdown_model"] == "mineru-vlm"
    # No Datalab verdict on the MinerU path: coalesce keeps any prior score.
    assert captured["params"]["parse_quality"] is None


def test_persist_records_datalab_provenance(monkeypatch):
    captured = _capture_run_query(monkeypatch)
    B._persist_reingest_result("n.jpg", markdown="MD", blocks_json="{}",
                               markdown_raw=None, blocks_raw=None,
                               markdown_source="datalab",
                               markdown_model="datalab-fast",
                               parse_quality=4.0)
    assert captured["params"]["markdown_source"] == "datalab"
    assert captured["params"]["markdown_model"] == "datalab-fast"
    assert captured["params"]["parse_quality"] == 4.0
    # A hardcoded engine string would defeat the parameter entirely.
    assert "'mineru'" not in captured["cypher"]


# ── the full single-file re-ingest, per engine ──────────────────────────────

DATALAB_BLOCKS = [
    {"id": "", "page": 1, "bbox": [0.1, 0.05, 0.9, 0.12], "label": "Title",
     "text": "NOTICE OF SALE", "reading_order": 1000, "source": "datalab",
     "table": None},
    {"id": "", "page": 1, "bbox": [0.1, 0.15, 0.9, 0.30], "label": "Text",
     "text": "The undersigned has taken possession…", "reading_order": 1001,
     "source": "datalab", "table": None},
    {"id": "", "page": 1, "bbox": [0.1, 0.32, 0.9, 0.80], "label": "Table",
     "text": "<table><tr><td>Reserve Price</td></tr></table>",
     "reading_order": 1002, "source": "datalab", "table": {"format": "html"}},
]

MINERU_BLOCKS = [
    {"id": "", "page": 1, "bbox": [0.05, 0.03, 0.95, 0.97], "label": "Table",
     "text": "<table><tr><td>the whole notice</td></tr></table>",
     "reading_order": 1000, "source": "mineru", "table": {"format": "html"}},
]


@pytest.fixture
def reingest_harness(monkeypatch, tmp_path):
    """Stub every boundary ``reingest_notice`` touches; record what ran."""
    calls: dict = {}

    monkeypatch.setattr(B, "run_read_query", lambda *a, **k: [{
        "file_path": "notices/1/n.jpg", "filename": "n.jpg",
        "public_url": None, "notice_type": "single",
        "crop_bbox": None, "crop_page": None, "crop_regions_json": None,
        "rotation": 0,
    }])
    monkeypatch.setattr(B, "run_query", lambda *a, **k: [{"rev": 1}])

    src = tmp_path / "n.jpg"
    src.write_bytes(b"jpegbytes")
    md_path = tmp_path / "n.md"
    md_path.write_text("# NOTICE OF SALE\n\nbody", encoding="utf-8")
    blocks_path = tmp_path / "n.json"

    import pipeline.mineru as MU
    monkeypatch.setattr(MU, "find_disk_path", lambda name: src)
    monkeypatch.setattr(MU, "read_mineru_meta",
                        lambda fp: {"zip_url": "https://r2/stale-mineru.zip",
                                    "img_map": {}})

    import pipeline.datalab_api as DLA

    def _run_and_cache(file_path, disk_path, *, mode="fast", **kw):
        calls["engine"] = "datalab"
        calls["mode"] = mode
        calls["disk_path"] = Path(disk_path)
        blocks_path.write_text(json.dumps({"blocks": DATALAB_BLOCKS}),
                               encoding="utf-8")
        return md_path, blocks_path

    monkeypatch.setattr(DLA, "run_and_cache", _run_and_cache)

    import pipeline.mineru_api as MA
    monkeypatch.setattr(MA, "request_batch", lambda items: ("b1", ["u"]))
    monkeypatch.setattr(MA, "upload_files", lambda items, urls: None)
    monkeypatch.setattr(MA, "poll", lambda bid: [
        {"state": "done", "full_zip_url": "https://r2/fresh.zip"}])

    def _download_and_cache(fp, zip_url, archive_to_r2=False):
        calls["engine"] = "mineru"
        blocks_path.write_text(json.dumps({"blocks": MINERU_BLOCKS}),
                               encoding="utf-8")
        return md_path, blocks_path

    monkeypatch.setattr(MA, "download_and_cache", _download_and_cache)

    import pipeline.load_markdowns_to_neo4j as LD
    monkeypatch.setattr(LD, "read_parse_quality", lambda fp: 4.5)

    def _load_blocks_for(fp, img_map=None):
        raw = json.loads(blocks_path.read_text(encoding="utf-8"))
        return [dict(b, id=b["id"] or f"blk{i}")
                for i, b in enumerate(raw["blocks"])]

    monkeypatch.setattr(LD, "load_blocks_for", _load_blocks_for)

    persisted: dict = {}
    monkeypatch.setattr(B, "_persist_reingest_result",
                        lambda filename, **kw: persisted.update(kw))
    monkeypatch.setattr(B, "get_blocks", lambda filename: {"ok": True})

    # Post-write scoring hits Neo4j; not what these tests are about.
    import pipeline.score_markdown as SM
    import pipeline.ocr_health as OH
    monkeypatch.setattr(SM, "score_freshly_loaded", lambda fps: 0)
    monkeypatch.setattr(OH, "score_freshly_loaded", lambda fps: 0)

    return calls, persisted


def test_reingest_with_datalab_returns_multiple_blocks(reingest_harness):
    """The bug: a fully-ruled notice came back as ONE Table block."""
    calls, persisted = reingest_harness
    B.reingest_notice("n.jpg", "a@b.com", engine="datalab")

    assert calls["engine"] == "datalab"
    blocks = json.loads(persisted["blocks_json"])["blocks"]
    assert len(blocks) == 3
    assert [b["label"] for b in blocks] == ["Title", "Text", "Table"]
    assert persisted["markdown_source"] == "datalab"
    assert persisted["markdown_model"] == "datalab-fast"
    assert persisted["parse_quality"] == 4.5
    # A previous MinerU run's archived zip must not be re-stamped as this run's.
    assert persisted["mineru_zip_url"] is None


def test_reingest_with_mineru_still_uses_mineru(reingest_harness):
    calls, persisted = reingest_harness
    B.reingest_notice("n.jpg", "a@b.com", engine="mineru")

    assert calls["engine"] == "mineru"
    assert persisted["markdown_source"] == "mineru"
    assert persisted["markdown_model"] == "mineru-vlm"
    assert persisted["parse_quality"] is None
    assert persisted["mineru_zip_url"] == "https://r2/stale-mineru.zip"


def test_reingest_defaults_to_the_pipeline_engine(reingest_harness, monkeypatch):
    import pipeline.config as CFG
    monkeypatch.setattr(CFG, "DESCRIPTION_OCR_ENGINE", "datalab")
    calls, _ = reingest_harness
    B.reingest_notice("n.jpg", "a@b.com")
    assert calls["engine"] == "datalab"


def test_datalab_tier_follows_notice_type(reingest_harness, monkeypatch):
    """Multi-lot notices are long and dense — they need the accurate tier."""
    calls, _ = reingest_harness
    monkeypatch.setattr(B, "run_read_query", lambda *a, **k: [{
        "file_path": "notices/1/n.jpg", "filename": "n.jpg",
        "public_url": None, "notice_type": "multi",
        "crop_bbox": None, "crop_page": None, "crop_regions_json": None,
        "rotation": 0,
    }])
    B.reingest_notice("n.jpg", "a@b.com", engine="datalab")
    assert calls["mode"] == "accurate"


# ── multi-region crops, per engine ──────────────────────────────────────────

def _datalab_payload(text: str) -> dict:
    return {
        "json": {"block_type": "Document", "children": [
            {"block_type": "Page", "bbox": [0, 0, 1000, 400], "children": [
                {"block_type": "SectionHeader", "html": f"<h1>{text}</h1>",
                 "bbox": [10, 10, 990, 60]},
                {"block_type": "Text", "html": f"<p>{text} body</p>",
                 "bbox": [10, 70, 990, 390]},
            ]},
        ]},
        "parse_quality_score": 3.5,
    }


def test_multi_region_reingest_runs_datalab_per_crop(monkeypatch, tmp_path):
    """Regions are the documented remedy for table-collapse; they must be
    OCR-able with Datalab too, not only MinerU."""
    src = tmp_path / "n.jpg"
    src.write_bytes(b"jpegbytes")

    import pipeline.datalab_api as DLA
    import pipeline.reextract as RX
    monkeypatch.setattr(RX, "_image_crop_to_png", lambda b, bbox: b"crop")

    seen: list = []

    def _run_file(disk_path, *, output_format="json", mode="fast", **kw):
        seen.append(mode)
        return _datalab_payload(f"region{len(seen)}")

    monkeypatch.setattr(DLA, "run_file", _run_file)

    persisted: dict = {}
    monkeypatch.setattr(B, "_persist_reingest_result",
                        lambda filename, **kw: persisted.update(kw))
    monkeypatch.setattr(B, "get_blocks", lambda filename: {"ok": True})

    import pipeline.score_markdown as SM
    import pipeline.ocr_health as OH
    monkeypatch.setattr(SM, "score_freshly_loaded", lambda fps: 0)
    monkeypatch.setattr(OH, "score_freshly_loaded", lambda fps: 0)

    import pipeline.mineru as MU
    monkeypatch.setattr(MU, "MINERU_MARKDOWN_DIR", tmp_path / "md")
    monkeypatch.setattr(MU, "MINERU_BLOCKS_DIR", tmp_path / "bl")
    monkeypatch.setattr(MU, "write_mineru_meta", lambda fp, meta: None)

    regions = [{"page": 1, "bbox": [0.0, 0.0, 1.0, 0.5]},
               {"page": 1, "bbox": [0.0, 0.5, 1.0, 1.0]}]
    B._reingest_multi_region(
        filename="n.jpg", fp="notices/1/n.jpg", src_filename="n.jpg",
        disk=src, regions=regions, applied_rotation=0, effective_page=1,
        engine="datalab", notice_type="multi",
    )

    assert seen == ["accurate", "accurate"], "one Datalab call per region"
    blocks = json.loads(persisted["blocks_json"])["blocks"]
    # Two regions x two blocks each, merged into full-image coords.
    assert len(blocks) == 4
    assert [b["label"] for b in blocks] == ["Title", "Text", "Title", "Text"]
    assert all(b["id"] for b in blocks), "every merged block needs an id"
    # Region 2's blocks live in the bottom half of the page.
    assert blocks[2]["bbox"][1] >= 0.5
    assert persisted["markdown_source"] == "datalab"
    assert persisted["markdown_model"] == "datalab-accurate"
    assert persisted["parse_quality"] == 3.5
    # blocks_raw is the flat list of every region's canonical blocks.
    assert len(json.loads(persisted["blocks_raw"])) == 4


def test_multi_region_datalab_aborts_when_a_region_reads_empty(monkeypatch,
                                                               tmp_path):
    """All-or-nothing: a band that OCRs to nothing must not silently vanish."""
    src = tmp_path / "n.jpg"
    src.write_bytes(b"jpegbytes")

    import pipeline.datalab_api as DLA
    import pipeline.reextract as RX
    monkeypatch.setattr(RX, "_image_crop_to_png", lambda b, bbox: b"crop")

    calls = {"n": 0}

    def _run_file(disk_path, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            return {"json": {"block_type": "Document", "children": []}}
        return _datalab_payload("region1")

    monkeypatch.setattr(DLA, "run_file", _run_file)
    monkeypatch.setattr(B, "_persist_reingest_result",
                        lambda *a, **k: pytest.fail("must not persist"))

    with pytest.raises(RuntimeError, match="no blocks for crop region 2"):
        B._reingest_multi_region(
            filename="n.jpg", fp="notices/1/n.jpg", src_filename="n.jpg",
            disk=src, regions=[{"page": 1, "bbox": [0.0, 0.0, 1.0, 0.5]},
                               {"page": 1, "bbox": [0.0, 0.5, 1.0, 1.0]}],
            applied_rotation=0, effective_page=1,
            engine="datalab", notice_type="single",
        )


# ── the engine reaches the background task ──────────────────────────────────

def test_reingest_safe_forwards_the_engine(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(B, "reingest_notice",
                        lambda fn, by, engine=None: seen.update(engine=engine))
    B.reingest_notice_safe("n.jpg", "a@b.com", "mineru")
    assert seen["engine"] == "mineru"


def _reingest_client(monkeypatch, scheduled):
    """TestClient over the real app with auth + the background task stubbed.

    Goes through FastAPI itself: the body model, the response model and the
    background-task signature are three places this can be wired wrong that a
    direct call to the handler would not catch.
    """
    from fastapi.testclient import TestClient

    import importlib

    from api.auth import get_current_admin
    from api.main import app

    # api/review/__init__.py rebinds `router` to the APIRouter object, which
    # shadows the submodule — `import api.review.router as R` would hand back
    # the APIRouter, not the module.
    R = importlib.import_module("api.review.router")

    monkeypatch.setattr(R.block_ops, "get_blocks",
                        lambda filename: {"blocks_revision": 46})
    monkeypatch.setattr(
        R.block_ops, "reingest_notice_safe",
        lambda filename, by_email, engine=None: scheduled.update(
            filename=filename, engine=engine))

    class _Admin:
        email = "a@b.com"

    app.dependency_overrides[get_current_admin] = lambda: _Admin()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _datalab_key(monkeypatch):
    """Pin a key so the endpoint's fast-fail guard doesn't make these tests
    pass or fail on whether the machine happens to have DATALAB_API_KEY set.
    The one test that cares about the missing-key path overrides this."""
    monkeypatch.setattr("pipeline.datalab_api.DATALAB_API_KEY", "test-key")


def test_router_forwards_the_requested_engine(monkeypatch):
    scheduled: dict = {}
    client = _reingest_client(monkeypatch, scheduled)
    try:
        r = client.post("/review/notice/n.jpg/reingest",
                        json={"engine": "datalab"})
        assert r.status_code == 202, r.text
        assert r.json()["engine"] == "datalab"
        assert r.json()["blocks_revision"] == 46
        assert scheduled["engine"] == "datalab"
    finally:
        from api.main import app
        app.dependency_overrides.clear()


def test_router_resolves_a_bodyless_post_to_a_concrete_engine(monkeypatch):
    """The endpoint took no body before; old callers must keep working."""
    scheduled: dict = {}
    client = _reingest_client(monkeypatch, scheduled)
    try:
        r = client.post("/review/notice/n.jpg/reingest")
        assert r.status_code == 202, r.text
        assert scheduled["engine"] in B.REINGEST_ENGINES
        assert r.json()["engine"] == scheduled["engine"]
    finally:
        from api.main import app
        app.dependency_overrides.clear()


def test_router_rejects_nothing_but_normalizes_junk(monkeypatch):
    """An unknown engine name degrades to the default, never a 500."""
    scheduled: dict = {}
    client = _reingest_client(monkeypatch, scheduled)
    try:
        r = client.post("/review/notice/n.jpg/reingest",
                        json={"engine": "tesseract"})
        assert r.status_code == 202, r.text
        assert scheduled["engine"] in B.REINGEST_ENGINES
    finally:
        from api.main import app
        app.dependency_overrides.clear()


def test_router_fails_fast_when_the_datalab_key_is_missing(monkeypatch):
    """The 202 has no error channel — an unconfigured key must 503 here, not
    strand the reviewer on a 5-minute poll that never resolves."""
    scheduled: dict = {}
    monkeypatch.setattr("pipeline.datalab_api.DATALAB_API_KEY", "")
    client = _reingest_client(monkeypatch, scheduled)
    try:
        r = client.post("/review/notice/n.jpg/reingest",
                        json={"engine": "datalab"})
        assert r.status_code == 503, r.text
        assert "DATALAB_API_KEY" in r.json()["detail"]
        assert not scheduled, "nothing should have been scheduled"
        # MinerU is unaffected by a missing Datalab key.
        r = client.post("/review/notice/n.jpg/reingest",
                        json={"engine": "mineru"})
        assert r.status_code == 202, r.text
    finally:
        from api.main import app
        app.dependency_overrides.clear()


def test_reingest_docstring_is_not_mineru_only():
    """Guards the doc drift that hid this bug: the button was labelled and
    documented as a MinerU re-run, so the engine picker looked out of scope."""
    src = inspect.getsource(B.reingest_notice)
    assert "datalab" in src.lower()
