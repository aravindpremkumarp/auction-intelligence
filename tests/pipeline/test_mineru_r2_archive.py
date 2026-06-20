"""R2 archival of MinerU's full output.

``archive_zip_to_r2`` uploads the complete result zip plus every image/table
crop inside it, returns a {basename -> URL} map + zip URL, and writes the meta
sidecar. ``download_and_cache`` invokes it only when ``archive_to_r2`` is set.
Best-effort: an R2 failure must never raise out of an OCR run.

DB-free; R2 and cache dirs are stubbed/redirected. See
docs/superpowers/specs/2026-06-20-keep-full-mineru-output-design.md.
"""
from __future__ import annotations

import io
import zipfile

import pipeline.mineru as M
import pipeline.mineru_api as api
import pipeline.storage as S


def _make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("full.md", "# notice")
        z.writestr("doc_content_list.json",
                   '[{"type":"image","img_path":"images/aa.jpg"}]')
        z.writestr("images/aa.jpg", b"\xff\xd8jpegbytes")
        z.writestr("images/bb.jpg", b"\xff\xd8more")
    return buf.getvalue()


def _stub_storage(monkeypatch, uploaded):
    monkeypatch.setattr(S, "upload_bytes",
                        lambda key, body, ct=None: uploaded.append((key, body, ct))
                        or f"https://cdn/{key}")
    monkeypatch.setattr(S, "mineru_zip_key", lambda safe: f"mineru/raw_zips/{safe}.zip")
    monkeypatch.setattr(S, "mineru_image_key",
                        lambda safe, base: f"mineru/images/{safe}/{base}")
    monkeypatch.setattr(S, "guess_content_type", lambda name: "image/jpeg")


def test_archive_uploads_zip_and_images_and_writes_meta(tmp_path, monkeypatch):
    uploaded = []
    _stub_storage(monkeypatch, uploaded)
    monkeypatch.setattr(M, "MINERU_META_DIR", tmp_path / "meta")

    fp = "notices/123/x.jpg"
    meta = api.archive_zip_to_r2(fp, _make_zip())

    keys = [k for (k, _b, _c) in uploaded]
    assert any(k.endswith(".zip") for k in keys)          # whole zip archived
    assert sum(1 for k in keys if "/images/" in k) == 2   # both crops archived
    assert meta["zip_url"].endswith(".zip")
    assert set(meta["img_map"]) == {"aa.jpg", "bb.jpg"}   # keyed by basename

    # Sidecar persisted and reloadable.
    reloaded = M.read_mineru_meta(fp)
    assert reloaded["img_map"]["aa.jpg"] == meta["img_map"]["aa.jpg"]
    assert reloaded["zip_url"] == meta["zip_url"]


def test_archive_best_effort_when_upload_fails(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise S.R2ConfigError("no creds")
    monkeypatch.setattr(S, "upload_bytes", _boom)
    monkeypatch.setattr(M, "MINERU_META_DIR", tmp_path / "meta")

    # Must not raise — a storage problem can't fail the OCR run.
    meta = api.archive_zip_to_r2("notices/x.jpg", _make_zip())
    assert meta["zip_url"] is None
    assert meta["img_map"] == {}


def test_download_and_cache_archives_when_flag_set(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(api, "download_zip", lambda url, **k: _make_zip())
    monkeypatch.setattr(api, "MINERU_MARKDOWN_DIR", tmp_path / "md")
    monkeypatch.setattr(api, "MINERU_BLOCKS_DIR", tmp_path / "bl")
    monkeypatch.setattr(api, "archive_zip_to_r2",
                        lambda fp, zb: calls.setdefault("fp", fp))

    api.download_and_cache("notices/x.jpg", "http://zip", archive_to_r2=True)
    assert calls["fp"] == "notices/x.jpg"


def test_download_and_cache_skips_archive_by_default(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(api, "download_zip", lambda url, **k: _make_zip())
    monkeypatch.setattr(api, "MINERU_MARKDOWN_DIR", tmp_path / "md")
    monkeypatch.setattr(api, "MINERU_BLOCKS_DIR", tmp_path / "bl")
    monkeypatch.setattr(api, "archive_zip_to_r2",
                        lambda fp, zb: calls.setdefault("fp", fp))

    api.download_and_cache("notices/x.jpg", "http://zip")
    assert "fp" not in calls
