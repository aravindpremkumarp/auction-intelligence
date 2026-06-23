"""
tests/test_upload_canonical.py
------------------------------
Unit coverage for the canonical-reuse guard in scripts/upload_downloads_to_r2.

A canonical whose R2 object has vanished must NOT be reused — reusing it would
propagate a 404ing public_url to this auction (the multi-property-notice bug).
The script must instead fall through and re-upload from the local file under
this auction's own key.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import scripts.upload_downloads_to_r2 as up


def test_reuses_canonical_when_object_exists(monkeypatch):
    monkeypatch.setattr(up, "lookup_canonical", lambda fn: {
        "storage_key": "notices/B/x.jpg",
        "public_url": "https://r2/notices/B/x.jpg",
        "content_type": "image/jpeg",
    })
    monkeypatch.setattr(up.storage, "exists", lambda k: True)
    seen: dict = {}
    monkeypatch.setattr(up, "upsert_document", lambda **kw: seen.update(kw))

    def _boom(fn):
        raise AssertionError("must not touch local files on the reuse path")
    monkeypatch.setattr(up, "locate_local_file", _boom)

    res = up.UploadResult()
    up.process_auction("A", ["x.jpg"], dry_run=False, result=res)

    assert res.reused_canonical == 1
    assert seen["storage_key"] == "notices/B/x.jpg"


def test_falls_through_and_reuploads_when_canonical_object_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(up, "lookup_canonical", lambda fn: {
        "storage_key": "notices/B/x.jpg",
        "public_url": "https://r2/notices/B/x.jpg",
        "content_type": "image/jpeg",
    })
    # Neither the dangling canonical key nor the fresh per-auction key pre-exist.
    monkeypatch.setattr(up.storage, "exists", lambda k: False)

    local = tmp_path / "x.jpg"
    local.write_bytes(b"img")
    monkeypatch.setattr(up, "locate_local_file", lambda fn: local)

    uploaded: dict = {}
    def _upload(path, key, content_type):
        uploaded["key"] = key
        return f"https://r2/{key}"
    monkeypatch.setattr(up.storage, "upload_file", _upload)

    seen: dict = {}
    monkeypatch.setattr(up, "upsert_document", lambda **kw: seen.update(kw))

    res = up.UploadResult()
    up.process_auction("A", ["x.jpg"], dry_run=False, result=res)

    # Re-uploaded under THIS auction's own key, not the dangling canonical's.
    assert uploaded["key"] == "notices/A/x.jpg"
    assert seen["storage_key"] == "notices/A/x.jpg"
    assert res.reused_canonical == 0
    assert res.uploaded == 1
