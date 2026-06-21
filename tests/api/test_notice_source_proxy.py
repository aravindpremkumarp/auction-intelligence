"""
tests/api/test_notice_source_proxy.py
-------------------------------------
Tests for the resilient notice-source proxy — the serving-layer half of the
multi-property-notice 404 fix.

A notice file is shared across the lots of a multi-property notice; a given
Document.public_url can point at a per-auction key whose object was never
uploaded under that prefix. The proxy must gather every candidate key for the
filename and stream the first that resolves, so the review viewer self-heals.
"""
from __future__ import annotations

import importlib

import pytest

BASE = "https://pub-test.r2.dev"


def _router_mod():
    # api/review/__init__.py binds `router` (the APIRouter object) as an
    # attribute, which shadows the submodule name — so `import api.review.router`
    # would grab the object. importlib returns the real module.
    return importlib.import_module("api.review.router")


@pytest.fixture(autouse=True)
def _r2_base(monkeypatch):
    monkeypatch.setenv("R2_PUBLIC_BASE_URL", BASE)


def _client():
    from fastapi.testclient import TestClient
    from api.main import app
    return TestClient(app)


def _patch_graph(monkeypatch, rows):
    r = _router_mod()
    monkeypatch.setattr(
        r, "run_read_query",
        lambda cypher, params=None, timeout=10.0, max_rows=200: rows,
    )


def _row(urls=None, keys=None, auction_ids=None):
    return [{"urls": urls or [], "keys": keys or [], "auction_ids": auction_ids or []}]


class _FakeResp:
    def __init__(self, status, body=b"", content_type="image/jpeg"):
        self.status_code = status
        self.ok = 200 <= status < 400
        self._body = body
        self.headers = {"content-type": content_type}

    def iter_content(self, chunk_size=65536):
        yield self._body

    def close(self):
        pass


def test_candidates_built_from_urls_keys_and_auctions(monkeypatch):
    r = _router_mod()
    _patch_graph(monkeypatch, _row(
        urls=[f"{BASE}/notices/766628/x.jpg"],
        keys=["notices/766628/x.jpg"],
        auction_ids=["766627", "766628"],
    ))
    cands = r._notice_source_candidates("x.jpg")
    # Stored URL first (happy path), then the reconstructed per-auction keys.
    assert cands[0] == f"{BASE}/notices/766628/x.jpg"
    assert f"{BASE}/notices/766627/x.jpg" in cands
    assert len(cands) == len(set(cands))  # de-duplicated


def test_candidates_drop_urls_outside_base(monkeypatch):
    r = _router_mod()
    _patch_graph(monkeypatch, _row(
        urls=["https://evil.example/notices/a/x.jpg", f"{BASE}/notices/a/x.jpg"],
    ))
    cands = r._notice_source_candidates("x.jpg")
    assert cands == [f"{BASE}/notices/a/x.jpg"]  # SSRF: out-of-base dropped


def test_proxy_streams_first_working_candidate(monkeypatch):
    """Stored URL 404s; proxy self-heals to a sibling key that resolves."""
    _patch_graph(monkeypatch, _row(
        urls=[f"{BASE}/notices/766627/x.jpg"],   # dangling
        keys=["notices/766627/x.jpg"],
        auction_ids=["766627", "766628"],
    ))
    served = {f"{BASE}/notices/766628/x.jpg": b"JPEGBYTES"}
    import requests
    monkeypatch.setattr(
        requests, "get",
        lambda url, timeout=30, stream=True: _FakeResp(200, served[url])
        if url in served else _FakeResp(404),
    )
    resp = _client().get("/review/notice/x.jpg/source")
    assert resp.status_code == 200
    assert resp.content == b"JPEGBYTES"


def test_proxy_404_when_all_candidates_missing(monkeypatch):
    _patch_graph(monkeypatch, _row(
        urls=[f"{BASE}/notices/766627/x.jpg"],
        keys=["notices/766627/x.jpg"],
        auction_ids=["766627"],
    ))
    import requests
    monkeypatch.setattr(
        requests, "get",
        lambda url, timeout=30, stream=True: _FakeResp(404),
    )
    resp = _client().get("/review/notice/x.jpg/source")
    assert resp.status_code == 404


def test_proxy_404_when_no_document(monkeypatch):
    _patch_graph(monkeypatch, [])  # MATCH (d:Document {filename}) found nothing
    resp = _client().get("/review/notice/unknown.jpg/source")
    assert resp.status_code == 404
