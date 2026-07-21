"""pipeline.datalab_api: submit + poll against the hosted convert endpoint.

Network-free — requests.post/get and time.sleep are monkeypatched. Covers the
X-API-Key header + multipart params on submit, the processing→complete poll
transition, and the failure/timeout paths.
"""
from __future__ import annotations

import pytest

import pipeline.datalab_api as DLA


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(DLA.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(DLA, "DATALAB_API_KEY", "test-key")


def _tmp_pdf(tmp_path):
    p = tmp_path / "notice.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    return p


def test_submit_sends_key_and_params(tmp_path, monkeypatch):
    captured = {}

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["data"] = data
        captured["has_file"] = "file" in (files or {})
        return _Resp({"success": True, "request_id": "r1",
                      "request_check_url": "https://chk/r1"})

    monkeypatch.setattr(DLA.requests, "post", fake_post)

    rid, url = DLA.submit(_tmp_pdf(tmp_path), output_format="json", mode="fast")

    assert rid == "r1"
    assert url == "https://chk/r1"
    assert captured["url"] == DLA.DATALAB_CONVERT_URL
    assert captured["headers"]["X-API-Key"] == "test-key"
    assert captured["data"]["output_format"] == "json"
    assert captured["data"]["mode"] == "fast"
    assert captured["has_file"] is True


def test_submit_raises_without_key(tmp_path, monkeypatch):
    monkeypatch.setattr(DLA, "DATALAB_API_KEY", "")
    with pytest.raises(DLA.DatalabError):
        DLA.submit(_tmp_pdf(tmp_path))


def test_submit_raises_on_unsuccessful_body(tmp_path, monkeypatch):
    monkeypatch.setattr(DLA.requests, "post",
                        lambda *a, **k: _Resp({"success": False, "error": "bad file"}))
    with pytest.raises(DLA.DatalabError):
        DLA.submit(_tmp_pdf(tmp_path))


def test_poll_waits_then_returns_complete(monkeypatch):
    seq = iter([
        _Resp({"status": "processing"}),
        _Resp({"status": "processing"}),
        _Resp({"status": "complete", "success": True,
               "markdown": "# ok", "json": {"block_type": "Document", "children": []}}),
    ])
    monkeypatch.setattr(DLA.requests, "get", lambda *a, **k: next(seq))

    out = DLA.poll("https://chk/r1", timeout_s=30)
    assert out["status"] == "complete"
    assert out["markdown"] == "# ok"


def test_poll_raises_on_failed(monkeypatch):
    monkeypatch.setattr(DLA.requests, "get",
                        lambda *a, **k: _Resp({"status": "failed", "error": "boom"}))
    with pytest.raises(DLA.DatalabError):
        DLA.poll("https://chk/r1", timeout_s=30)


def test_poll_times_out(monkeypatch):
    monkeypatch.setattr(DLA.requests, "get",
                        lambda *a, **k: _Resp({"status": "processing"}))
    with pytest.raises(TimeoutError):
        DLA.poll("https://chk/r1", timeout_s=0)


def test_run_file_submits_then_polls(tmp_path, monkeypatch):
    monkeypatch.setattr(DLA.requests, "post",
                        lambda *a, **k: _Resp({"success": True, "request_id": "r1",
                                               "request_check_url": "https://chk/r1"}))
    monkeypatch.setattr(DLA.requests, "get",
                        lambda *a, **k: _Resp({"status": "complete", "success": True,
                                               "markdown": "# done", "json": {}}))
    out = DLA.run_file(_tmp_pdf(tmp_path), output_format="json", mode="fast")
    assert out["markdown"] == "# done"


def test_extract_payload_splits_fields():
    md, doc, images = DLA.extract_payload(
        {"markdown": "# m", "json": {"block_type": "Document"}, "images": {"a": "b64"}})
    assert md == "# m"
    assert doc == {"block_type": "Document"}
    assert images == {"a": "b64"}
